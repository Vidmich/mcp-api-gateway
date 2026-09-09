"""Pushing the usage counters to a monitoring service, when asked to (task 125).

The gateway counts everything it does and shows those counts to one operator, on
one page, in one browser. An operator who already runs a monitoring system wants
them where the rest of their infrastructure is — so that a gateway going quiet is
noticed by the thing that notices everything else going quiet. This is that push:
optional, off unless somebody turns it on, and absent while it is off.

**It reads the table rather than the writer.** :class:`~mcp_gateway.metrics.
MetricsWriter` drains its counters every ten seconds and could hand the same
deltas here for nothing. It should not. The monitoring page and the destination
are then two foldings of the same rows, so a chart saying 400 calls beside a
dashboard saying 380 is impossible rather than unlikely; a flush that fails drops
its window deliberately, and an export teed off the flush would send a window the
gateway itself does not believe in; a restart resumes where it left off; and
neither half can be held up by the other.

**A watermark, not a queue.** One ``settings`` row remembers the last bucket start
that was accepted. A pass sends what is after it, and moves it only once the far
end has said yes. Nothing is buffered in memory: the rows are in the table until
retention deletes them, so a destination down for an hour catches up completely
and one down for longer than ``metrics.retention_days`` loses the oldest — which
is the right trade for a process that must not grow a queue it cannot bound.

**Only closed buckets.** A bucket whose window has not ended is still being added
to, and sending it early would send the same minute twice with different numbers.
A pass therefore stops one bucket plus two flush intervals short of now.

**The key is read when it is needed and not before.** What lives on
``app.state.export`` is the shape of the export — where to, as what, and whether
there is a key at all — and never the key itself: that is decrypted inside the
pass that sends with it, so no template, no error page and no ``repr`` of the
application can reach a secret that only one loop has any use for.

**What leaves the process is counts.** Bucket start, the five counters, the kind,
and the server's id and display name. Never a tool name, never a call's
arguments, never a response, never an upstream URL, never a credential, and
nothing at all from ``call_errors``. The tables this reads were written to keep
request content out of them; this keeps it out of what leaves the machine.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import gzip
import itertools
import json
import logging
from collections.abc import AsyncIterator, Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import ClassVar, Final, Literal, Protocol

import httpx
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession

from mcp_gateway.config import Settings
from mcp_gateway.crypto import CredentialCipher, SecretUnreadable
from mcp_gateway.db import repo
from mcp_gateway.db.models import utcnow
from mcp_gateway.db.repo import MetricRow
from mcp_gateway.db.session import Database
from mcp_gateway.metrics import FLUSH_SECONDS

logger = logging.getLogger(__name__)

#: The ``settings`` rows the export lives in. Spelled like the config keys they
#: override, for the reason :data:`mcp_gateway.web.account.ENABLED_KEY` is: the
#: file and the page must not end up calling one setting two things.
DESTINATION_KEY: Final = "export.destination"
REGION_KEY: Final = "export.region"
SERVICE_NAME_KEY: Final = "export.service_name"
API_KEY_KEY: Final = "export.api_key"
#: The last bucket start a destination has accepted. Not a setting an operator
#: sets — it is this module's own memory — but it belongs in the same table for
#: the same reason: it has to survive a restart and it is one small value.
WATERMARK_KEY: Final = "export.exported_through"
EXPORT_KEYS: Final = (DESTINATION_KEY, REGION_KEY, SERVICE_NAME_KEY, API_KEY_KEY, WATERMARK_KEY)

#: The one destination there is, and the value that means there is none.
NEWRELIC: Final = "newrelic"
OFF: Final = ""

#: New Relic's two ingest endpoints. Which one an account belongs to is decided
#: when the account is created and cannot be worked out from the key.
ENDPOINTS: Final[dict[str, str]] = {
    "us": "https://metric-api.newrelic.com/metric/v1",
    "eu": "https://metric-api.eu.newrelic.com/metric/v1",
}

#: What every point says it came from, and what every metric name starts with.
PROVIDER: Final = "mcp-api-gateway"
PREFIX: Final = "mcp.gateway."

#: The bucket's five counters, and what each is called at the far end.
COUNTERS: Final[tuple[tuple[str, str], ...]] = (
    ("calls", "calls"),
    ("errors", "errors"),
    ("bytes_out", "bytes.out"),
    ("bytes_in", "bytes.in"),
    ("duration_ms_sum", "duration.ms"),
)

#: How long after the end of a bucket's window it is certainly written. Two
#: flushes rather than one, so a writer that was busy for a cycle still lands
#: its rows before the pass that would otherwise declare the window finished.
SETTLE_SECONDS: Final = 2 * FLUSH_SECONDS

#: How long after startup the first pass waits. Short, because the point of it
#: is to tell an operator who has just saved a key whether the key works, but
#: not immediate: the process is still opening a database and starting four
#: other services.
FIRST_SECONDS: Final = 15.0

#: The longest a failing export waits between attempts.
MAX_BACKOFF_SECONDS: Final = 15 * 60.0

#: How many stored rows one pass will read. A bound on catch-up rather than on
#: correctness: what is not read now is read next time.
ROWS_PER_PASS: Final = 5000

#: How many points one request will carry. New Relic caps a request by size;
#: this caps it by count, well under that, so no payload has to be measured
#: twice to find out whether it fits.
POINTS_PER_REQUEST: Final = 2000

#: How much of a rejection's body is quoted back to the operator.
MAX_DETAIL: Final = 200

#: Said when the export is on and the stored key cannot be decrypted — a keys.json
#: that was replaced, or a row edited by hand.
KEY_UNREADABLE: Final = (
    "The stored licence key cannot be read; enter it again on the Configuration page."
)
#: Said when the export is on and there is no key to send with at all.
KEY_MISSING: Final = "There is no licence key to send with."

#: What one pass can conclude. ``rejected`` is the odd one: the window is left
#: behind rather than retried, because a payload the destination will never
#: accept must not become the window after which nothing is ever exported.
Outcome = Literal["accepted", "retry", "rejected", "unauthorized"]


@dataclass(frozen=True, slots=True)
class Sent:
    """What one request came back as: what to do next, and what to say."""

    outcome: Outcome
    detail: str = ""


class Destination(Protocol):
    """Where counters go. One implementation today; the seam for a second.

    Small on purpose. Everything a destination needs to know about *this*
    gateway arrives as arguments, so a second one is a class here and a value in
    :data:`ENDPOINTS`' place — not a change to the loop, the watermark, or the
    page.
    """

    name: ClassVar[str]

    @property
    def endpoint(self) -> str:
        """The URL a pass posts to."""

    def body(
        self,
        rows: Sequence[MetricRow],
        *,
        service_name: str,
        bucket_seconds: int,
        names: Mapping[int, str],
    ) -> bytes:
        """One request's worth of rows, encoded as that destination wants them."""

    async def send(self, client: httpx.AsyncClient, body: bytes) -> Sent:
        """Post one body, and say what happened in terms the loop understands."""


def counters(row: MetricRow) -> Iterator[tuple[str, int]]:
    """What one bucket has to say, as ``(metric name, value)`` pairs.

    ``calls`` always, because a bucket exists to record that a number of calls
    happened and a series with holes in it is one nobody can read. The other
    four only when they are not zero: a ``throttled`` row has nothing but calls
    by definition (spec §4), a ``tools_list`` row moves no bytes, and four
    zeroes per row per minute is four times the ingest for no information.
    """
    for column, name in COUNTERS:
        value: int = getattr(row, column)
        if column == "calls" or value:
            yield PREFIX + name, value


def points_in(rows: Sequence[MetricRow]) -> int:
    """How many points these rows will turn into."""
    return sum(1 for row in rows for _ in counters(row))


def whole_buckets(rows: list[MetricRow], *, limit: int) -> list[MetricRow]:
    """Drop a trailing bucket start that the row limit may have cut in half.

    The watermark advances to a bucket start, so half of one counted as sent
    would lose the other half for good. A pass that read fewer rows than it
    asked for reached the end of the table and has nothing to trim.
    """
    if len(rows) < limit:
        return rows
    last = rows[-1].bucket_start
    kept = [row for row in rows if row.bucket_start != last]
    if kept:
        return kept
    # One bucket start with more rows in it than a whole pass reads: more
    # distinct server-and-kind series in one minute than a gateway can have.
    # Sending them is still better than never getting past them.
    logger.warning(
        "One metric bucket (%s) fills a whole export pass; some of it may not be sent",
        last,
    )
    return rows


def batches(rows: Sequence[MetricRow], *, points_per_request: int) -> Iterator[list[MetricRow]]:
    """Rows in request-sized pieces, never splitting one bucket start in two.

    The watermark moves to the last bucket start of each accepted piece, so a
    piece ending halfway through a timestamp would record that timestamp as sent
    while some of its rows were still waiting. Grouping by bucket start first
    makes that impossible rather than unlikely.
    """
    batch: list[MetricRow] = []
    size = 0
    for _, group in itertools.groupby(rows, key=lambda row: row.bucket_start):
        together = list(group)
        cost = points_in(together)
        if batch and size + cost > points_per_request:
            yield batch
            batch, size = [], 0
        batch.extend(together)
        size += cost
    if batch:
        yield batch


@dataclass(frozen=True, slots=True)
class NewRelic:
    """New Relic's Metric API: JSON, gzipped, one key in one header.

    Chosen over OTLP, which would be vendor-neutral and is the obvious second
    thing to support. The payload here is a transcription of a stored row and
    needs nothing this project does not already have; OTLP means either an SDK
    and its dependency tree or hand-rolled protobuf, for five counters.
    """

    name: ClassVar[str] = NEWRELIC

    region: str = "us"
    api_key: str = ""

    @property
    def endpoint(self) -> str:
        return ENDPOINTS[self.region]

    @property
    def host(self) -> str:
        """What a failure names, so an operator knows which end went wrong."""
        return httpx.URL(self.endpoint).host

    @property
    def headers(self) -> dict[str, str]:
        return {
            "Api-Key": self.api_key,
            "Content-Type": "application/json",
            "Content-Encoding": "gzip",
        }

    def attributes(self, row: MetricRow, names: Mapping[int, str]) -> dict[str, object]:
        """What one row's points are labelled with.

        A row whose server has since been deleted keeps its id and loses its
        name, which is the same thing the charts do with it: the number happened
        and the label for it did not survive.
        """
        attributes: dict[str, object] = {"kind": row.kind}
        if row.server_id is not None:
            attributes["server.id"] = row.server_id
            name = names.get(row.server_id)
            if name is not None:
                attributes["server"] = name
        return attributes

    def payload(
        self,
        rows: Sequence[MetricRow],
        *,
        service_name: str,
        bucket_seconds: int,
        names: Mapping[int, str],
    ) -> list[dict[str, object]]:
        """The request, as the objects that will be serialised.

        Every point is a ``count`` over the interval the bucket covers, which is
        what these numbers are: a total of things that happened in one window,
        not a reading taken at its end. Duration goes as a sum of milliseconds
        rather than as a ``summary``, because a summary wants a minimum and a
        maximum and the bucket stores neither; a mean is ``duration.ms / calls``
        at the far end, which is exactly what it is here.
        """
        interval = bucket_seconds * 1000
        metrics: list[dict[str, object]] = []
        for row in rows:
            timestamp = int(row.bucket_start.timestamp())
            attributes = self.attributes(row, names)
            metrics.extend(
                {
                    "name": name,
                    "type": "count",
                    "value": value,
                    "timestamp": timestamp,
                    "interval.ms": interval,
                    "attributes": attributes,
                }
                for name, value in counters(row)
            )
        common = {
            "attributes": {
                "service.name": service_name,
                "instrumentation.provider": PROVIDER,
            }
        }
        return [{"common": common, "metrics": metrics}]

    def body(
        self,
        rows: Sequence[MetricRow],
        *,
        service_name: str,
        bucket_seconds: int,
        names: Mapping[int, str],
    ) -> bytes:
        document = self.payload(
            rows, service_name=service_name, bucket_seconds=bucket_seconds, names=names
        )
        # mtime=0 so two identical payloads compress to identical bytes, which
        # is one less thing for a test to have to look inside to compare.
        return gzip.compress(json.dumps(document).encode("utf-8"), mtime=0)

    async def send(self, client: httpx.AsyncClient, body: bytes) -> Sent:
        """Post one body and classify the answer.

        The four outcomes are four different things to do, not four severities:
        keep going, come back later, give up until the key changes, or write
        this window off. Nothing here retries on its own — the loop owns when
        the next attempt happens, and a retry hidden inside one request would be
        a request that ignores the interval it was scheduled under.
        """
        try:
            response = await client.post(self.endpoint, content=body, headers=self.headers)
        except httpx.HTTPError as exc:
            return Sent("retry", f"{type(exc).__name__} talking to {self.host}")

        status = response.status_code
        where = f"HTTP {status} from {self.host}"
        if status in (200, 202):
            return Sent("accepted")
        if status in (401, 403):
            return Sent("unauthorized", f"{where}; the key may be wrong")
        if status == 429 or status >= 500:
            return Sent("retry", where)
        return Sent("rejected", f"{where}: {_quote(response)}")


def _quote(response: httpx.Response) -> str:
    """A rejection's own words, trimmed to something a page can hold."""
    try:
        text = response.text
    except (UnicodeDecodeError, httpx.HTTPError):  # pragma: no cover - defensive
        return "unreadable response body"
    text = " ".join(text.split())
    return text[:MAX_DETAIL] if text else "no message"


@dataclass(frozen=True, slots=True)
class StoredExport:
    """What the ``settings`` table says about the export.

    Only ever built when the table has said something. A caller holding ``None``
    instead is holding "the table is silent", which is what makes ``[export]``
    from the config file apply — the same rule, and for the same reasons, as
    :func:`mcp_gateway.web.account.stored_admin`.

    The key is *whether*, never *what*: reading it is a decryption, and the only
    thing with any use for the plaintext is the pass that sends with it.
    """

    #: ``""`` means the operator turned the export off, whatever the file says.
    destination: str
    region: str = "us"
    service_name: str = PROVIDER
    has_key: bool = False


@dataclass(frozen=True, slots=True)
class ExportConfig:
    """The export as it is actually in force, minus the secret.

    What sits on ``app.state.export``, resolved once as the gateway starts and
    again whenever the page writes. Everything that asks reads it per request or
    per pass, which is what lets the page change it without a restart.
    """

    destination: str = OFF
    region: str = "us"
    service_name: str = PROVIDER
    interval_seconds: int = 60
    #: Whether a key exists to send with. Not whether it can be decrypted —
    #: that is a question with a network call's worth of consequences, and it is
    #: answered by the pass, which reports it as a failure the page shows.
    has_key: bool = False
    #: Whether this came from the ``settings`` table rather than the file.
    stored: bool = False

    @property
    def enabled(self) -> bool:
        """Whether a pass would send anything. A destination with no key is not
        an export, it is a half-finished form."""
        return bool(self.destination) and self.has_key

    @property
    def summary(self) -> str:
        """One line for the startup banner. Never the key."""
        if not self.destination:
            return "off"
        where = f"{self.destination} ({self.region.upper()})"
        if not self.has_key:
            return f"{where}, but no licence key is set"
        every = f"every {self.interval_seconds}s"
        return f"{where}, {every}" + (" (set on the Configuration page)" if self.stored else "")


async def stored_export(session: AsyncSession) -> StoredExport | None:
    """Read the stored export, or ``None`` if the database has no opinion.

    Everything beside the destination is read whether the export is on or off,
    because :func:`store_off` leaves it there: an export switched off still has
    a key that can be forgotten and a region that does not have to be chosen
    again, and a reader that forgot both the moment the switch moved would make
    turning it back on a form to fill in from scratch.
    """
    destination = await repo.get_setting(session, DESTINATION_KEY)
    if destination is None:
        return None
    if destination not in (NEWRELIC, OFF):
        logger.error(
            "The stored export destination (%r) is not one this version knows about, "
            "so nothing is being exported. Set it again on the Configuration page.",
            destination,
        )
        destination = OFF
    # A region naming no endpoint is a row nothing wrote through the page, and
    # the loop could only fail on it. The default is the one an account gets
    # unless it asked otherwise.
    region = await repo.get_setting(session, REGION_KEY) or "us"
    if region not in ENDPOINTS:
        logger.error("The stored export region (%r) is not one of %s; using us.", region, "us/eu")
        region = "us"
    return StoredExport(
        destination=destination,
        region=region,
        service_name=await repo.get_setting(session, SERVICE_NAME_KEY) or PROVIDER,
        has_key=bool(await repo.get_setting(session, API_KEY_KEY)),
    )


def resolve(settings: Settings, stored: StoredExport | None) -> ExportConfig:
    """The export in force, given the file and whatever the table said.

    ``interval_seconds`` comes from the file either way. It is the one field the
    page does not offer, because how often to send is a property of the traffic
    and the ingest budget rather than something an operator changes while
    watching a dashboard — and a setting on a page nobody would touch is a
    setting that only makes the page longer.
    """
    export = settings.export
    if stored is None:
        return ExportConfig(
            destination=export.destination,
            region=export.region,
            service_name=export.service_name,
            interval_seconds=export.interval_seconds,
            has_key=bool(export.api_key),
            stored=False,
        )
    return ExportConfig(
        destination=stored.destination,
        region=stored.region,
        service_name=stored.service_name,
        interval_seconds=export.interval_seconds,
        has_key=stored.has_key,
        stored=True,
    )


async def load_export(session: AsyncSession, settings: Settings) -> ExportConfig:
    """Resolve the export against this database. The two steps above, together."""
    return resolve(settings, await stored_export(session))


async def store_export(
    session: AsyncSession,
    cipher: CredentialCipher | None,
    *,
    region: str,
    service_name: str,
    api_key: str | None,
) -> None:
    """Record an export that overrides ``[export]`` from the next pass on.

    ``api_key`` of ``None`` means "the operator did not retype it", which leaves
    whatever is stored exactly where it is — the same thing an empty credential
    box means on the server detail page. A key *with* no cipher to protect it is
    refused rather than written: a gateway built without an encryption key has
    nowhere safe to put this, and the clear is not the lesser of two evils.
    """
    if api_key is not None and cipher is None:
        raise ValueError("A licence key cannot be stored without an encryption key")
    await repo.set_setting(session, DESTINATION_KEY, NEWRELIC)
    await repo.set_setting(session, REGION_KEY, region)
    await repo.set_setting(session, SERVICE_NAME_KEY, service_name)
    if api_key is not None:
        assert cipher is not None  # refused above; here to satisfy the reader
        await repo.set_setting(session, API_KEY_KEY, cipher.encrypt_text(api_key))


async def store_off(session: AsyncSession) -> None:
    """Record that the export is off, whatever the config file says.

    The key stays, deliberately, and the page says so. This is the opposite of
    what switching admin login off does, and for a reason that survives being
    stated: a password hash for an account that is not in force is a verifier
    nothing can use, while a licence key is a value the operator would have to
    go back to another product to find again. Forgetting it is its own action.
    """
    await repo.set_setting(session, DESTINATION_KEY, OFF)


async def forget_key(session: AsyncSession) -> bool:
    """Delete the stored licence key.

    Without this there would be no way to take a secret off a gateway from the
    page that put it there, and "replace it with something wrong" is not a way.
    """
    return await repo.delete_setting(session, API_KEY_KEY)


async def read_key(
    session: AsyncSession,
    settings: Settings,
    config: ExportConfig,
    cipher: CredentialCipher | None,
) -> str:
    """The key to send with, read at the moment it is needed.

    Empty means there is nothing usable, which the caller reports rather than
    raising: a key that cannot be decrypted is an operator's problem to see on
    the page, not an exception in a background loop.
    """
    if not config.stored:
        return settings.export.api_key
    encrypted = await repo.get_setting(session, API_KEY_KEY)
    if not encrypted or cipher is None:
        return ""
    try:
        return cipher.decrypt_text(encrypted)
    except SecretUnreadable:
        return ""


async def read_watermark(session: AsyncSession) -> dt.datetime | None:
    """The last bucket start a destination accepted, or ``None`` for a fresh start."""
    written = await repo.get_setting(session, WATERMARK_KEY)
    if not written:
        return None
    try:
        at = dt.datetime.fromisoformat(written)
    except ValueError:
        logger.error(
            "The stored export watermark (%r) is not a timestamp; starting again from now.",
            written,
        )
        return None
    return at if at.tzinfo is not None else at.replace(tzinfo=dt.UTC)


async def write_watermark(session: AsyncSession, at: dt.datetime) -> None:
    """Remember that everything up to and including ``at`` has been accepted."""
    await repo.set_setting(session, WATERMARK_KEY, at.isoformat())


@dataclass(frozen=True, slots=True)
class Pass:
    """What one pass did, for a log line and for a test to read.

    Complete on its own, like a :class:`~mcp_gateway.retention.Purge`: it names
    what was sent, how far the watermark got and what went wrong, so nothing
    reading it has to go to the configuration to find out why.
    """

    at: dt.datetime
    rows: int = 0
    points: int = 0
    requests: int = 0
    #: How far the watermark stands after this pass.
    through: dt.datetime | None = None
    failure: str | None = None
    #: Whether the loop should stop until somebody changes something.
    stopped: bool = False

    @property
    def quiet(self) -> bool:
        """Whether this pass found nothing to do, which is most of them."""
        return not self.rows and self.failure is None

    @property
    def summary(self) -> str:
        return (
            f"sent {self.points} point(s) from {self.rows} bucket(s) in "
            f"{self.requests} request(s), through {self.through:%Y-%m-%d %H:%M}"
        )


@dataclass(frozen=True, slots=True)
class Status:
    """How the export is going, for the card that configured it.

    Held in memory and not written down. It describes this process's attempts,
    which is what an operator standing on the page is asking about; a history of
    them would be a second time series to retain and purge, to say something the
    destination itself can already be asked.
    """

    #: When a pass last completed without a failure, and what it carried.
    at: dt.datetime | None = None
    points: int = 0
    rows: int = 0
    #: The last failure, and when. Cleared by a pass that works.
    failure: str | None = None
    failed_at: dt.datetime | None = None
    #: Whether the loop has given up until the configuration changes.
    stopped: bool = False


class MetricsExport:
    """The lifespan task that pushes the counters (spec §8, task 125).

    Built around an app rather than a database for the reason the retention
    purge is: the database, the outbound client and the export configuration are
    all things with a lifetime, and reading them off ``app.state`` per pass is
    what lets this loop start before them, outlive them, and pick up a
    configuration the page changed underneath it.
    """

    def __init__(
        self,
        app: FastAPI,
        *,
        first_seconds: float = FIRST_SECONDS,
        rows_per_pass: int = ROWS_PER_PASS,
        points_per_request: int = POINTS_PER_REQUEST,
        max_backoff_seconds: float = MAX_BACKOFF_SECONDS,
        now: Callable[[], dt.datetime] = utcnow,
    ) -> None:
        self.app = app
        self.first_seconds = first_seconds
        self.rows_per_pass = rows_per_pass
        self.points_per_request = points_per_request
        self.max_backoff_seconds = max_backoff_seconds
        self._now = now
        self.status = Status()
        self._failures = 0
        self._wake = asyncio.Event()

    def __repr__(self) -> str:
        return f"{type(self).__name__}({self.config.summary})"

    @property
    def config(self) -> ExportConfig:
        """The export in force, read per pass rather than held."""
        config: ExportConfig | None = self.app.state.export
        return ExportConfig() if config is None else config

    def wake(self) -> None:
        """Run a pass now rather than at the next interval.

        What the Configuration page calls after a save, so that the answer to
        "is this key right" arrives in seconds on the page that asked rather
        than a minute later in a log. It also lifts a stop: a loop that gave up
        on a rejected key is exactly the loop a new key should restart.
        """
        self._failures = 0
        self.status = Status(
            at=self.status.at,
            points=self.status.points,
            rows=self.status.rows,
        )
        self._wake.set()

    def closed_through(self, now: dt.datetime, settings: Settings) -> dt.datetime:
        """The newest bucket start whose window has certainly been written.

        One bucket, because a window that has not ended is still being added to,
        plus two flush intervals, because a writer that was busy for a cycle
        has not written the window that did end.
        """
        margin = settings.metrics.bucket_seconds + SETTLE_SECONDS
        return now - dt.timedelta(seconds=margin)

    async def export(self) -> Pass:
        """One pass: read what is new, send it, and move the watermark.

        Reading and sending are deliberately in different transactions. A
        session held open across an HTTP request is a lock held for as long as
        somebody else's service takes to answer, and the whole point of pushing
        from the table rather than from the writer is that nothing here can slow
        down what is being counted.

        A pass with no database, no client or nothing configured is not an error
        and not a failure: it is startup, teardown, or an operator who has not
        turned this on.
        """
        now = self._now()
        config = self.config
        if not config.enabled:
            return Pass(at=now)

        database: Database | None = self.app.state.db
        client: httpx.AsyncClient | None = self.app.state.http_client
        if database is None or client is None:
            logger.debug("No database or client to export through; leaving it to the next pass")
            return Pass(at=now)

        settings: Settings = self.app.state.settings
        cipher: CredentialCipher | None = self.app.state.cipher
        until = self.closed_through(now, settings)

        async with database.session() as session:
            key = await read_key(session, settings, config, cipher)
            if not key:
                failure = KEY_UNREADABLE if config.stored else KEY_MISSING
                return Pass(at=now, failure=failure, stopped=True)
            watermark = await read_watermark(session)
            if watermark is None:
                # Switched on just now. Everything already in the table belongs
                # to before that, and a dashboard that starts with a month of
                # history delivered as one spike is not a dashboard starting.
                await write_watermark(session, until)
                logger.info("Metrics export starting from %s; earlier buckets are not sent", until)
                return Pass(at=now, through=until)
            names = await repo.server_names(session)
            rows = await repo.metric_rows_after(
                session, after=watermark, until=until, limit=self.rows_per_pass
            )

        rows = whole_buckets(rows, limit=self.rows_per_pass)
        if not rows:
            return Pass(at=now, through=watermark)

        destination = NewRelic(region=config.region, api_key=key)
        return await self._send(
            destination,
            client,
            database,
            rows,
            at=now,
            through=watermark,
            config=config,
            bucket_seconds=settings.metrics.bucket_seconds,
            names=names,
        )

    async def _send(
        self,
        destination: Destination,
        client: httpx.AsyncClient,
        database: Database,
        rows: Sequence[MetricRow],
        *,
        at: dt.datetime,
        through: dt.datetime,
        config: ExportConfig,
        bucket_seconds: int,
        names: Mapping[int, str],
    ) -> Pass:
        """Post the rows in pieces, moving the watermark behind each one.

        Per piece rather than per pass, so a destination that starts failing
        halfway through a catch-up keeps the ground the first half gained.
        """
        sent = points = requests = 0
        for batch in batches(rows, points_per_request=self.points_per_request):
            body = destination.body(
                batch,
                service_name=config.service_name,
                bucket_seconds=bucket_seconds,
                names=names,
            )
            result = await destination.send(client, body)
            requests += 1
            if result.outcome in ("retry", "unauthorized"):
                return Pass(
                    at=at,
                    rows=sent,
                    points=points,
                    requests=requests,
                    through=through,
                    failure=result.detail,
                    stopped=result.outcome == "unauthorized",
                )

            through = batch[-1].bucket_start
            async with database.session() as session:
                await write_watermark(session, through)
            if result.outcome == "accepted":
                sent += len(batch)
                points += points_in(batch)
            else:
                # Rejected: this process built something the destination will
                # not take, which is a bug here rather than a reason to send it
                # again every minute for the rest of the day.
                logger.error("Metrics export dropped %d bucket(s): %s", len(batch), result.detail)

        return Pass(at=at, rows=sent, points=points, requests=requests, through=through)

    def record(self, done: Pass) -> Status:
        """Fold a pass into the status, logging only what changed.

        A destination that has been down since yesterday is one line and a
        status, not one line per minute since yesterday.
        """
        was = self.status
        if done.failure is None:
            if was.failure is not None:
                logger.info("Metrics export is working again")
            self._failures = 0
            status = Status(at=done.at, points=done.points, rows=done.rows)
        else:
            if was.failure != done.failure:
                logger.warning("Metrics export is failing: %s", done.failure)
            if done.stopped and not was.stopped:
                logger.warning(
                    "Metrics export has stopped until the configuration changes: %s", done.failure
                )
            self._failures += 1
            status = Status(
                at=was.at,
                points=was.points,
                rows=was.rows,
                failure=done.failure,
                failed_at=done.at,
                stopped=done.stopped,
            )
        self.status = status
        return status

    def delay(self) -> float:
        """How long until the next pass: the interval, or a backed-off multiple."""
        interval = float(self.config.interval_seconds)
        if not self._failures:
            return interval
        return min(interval * 2.0**self._failures, self.max_backoff_seconds)

    async def pause(self, seconds: float) -> None:
        """Wait for the next pass, or for somebody to ask for one now."""
        try:
            await asyncio.wait_for(self._wake.wait(), seconds)
        except TimeoutError:
            return
        finally:
            self._wake.clear()

    async def run(self) -> None:
        """Export shortly after startup, then on the interval, until cancelled.

        It sleeps first either way, for the reason the retention purge does:
        the short wait is not idleness, it is letting startup finish before
        competing with it for the database.
        """
        logger.debug("Metrics export running: first pass in %gs", self.first_seconds)
        delay = self.first_seconds
        try:
            while True:
                await self.pause(delay)
                if self.status.stopped:
                    # Given up until the configuration changes, which is what
                    # wake() is. Still on the clock, so that a restarted export
                    # does not need the loop rebuilt around it.
                    delay = self.delay()
                    continue
                try:
                    done = await self.export()
                except Exception:
                    # One bad pass is not a reason to stop exporting for the
                    # life of the process; the watermark is where it was and
                    # the next pass sends the same rows.
                    logger.exception("The metrics export failed")
                    self._failures += 1
                else:
                    self.record(done)
                    if not done.quiet and done.failure is None:
                        logger.info("Metrics export %s", done.summary)
                delay = self.delay()
        except asyncio.CancelledError:
            logger.debug("Metrics export stopped")
            raise


@contextlib.asynccontextmanager
async def export_service(app: FastAPI) -> AsyncIterator[None]:
    """Resolve the export and run it for as long as the app does.

    A lifespan service in the sense of :mod:`mcp_gateway.app`. Resolution
    happens here rather than in ``create_app`` for the reason the admin
    account's does: the configuration may live in the ``settings`` table, and
    the table is not open until the database service has started.
    """
    settings: Settings = app.state.settings
    database: Database | None = app.state.db
    if database is not None:
        async with database.session() as session:
            app.state.export = await load_export(session, settings)

    export = MetricsExport(app)
    app.state.export_service = export
    task = asyncio.create_task(export.run(), name="metrics-export")
    try:
        yield
    finally:
        app.state.export_service = None
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


__all__ = [
    "API_KEY_KEY",
    "COUNTERS",
    "DESTINATION_KEY",
    "ENDPOINTS",
    "EXPORT_KEYS",
    "FIRST_SECONDS",
    "KEY_MISSING",
    "KEY_UNREADABLE",
    "MAX_BACKOFF_SECONDS",
    "NEWRELIC",
    "OFF",
    "POINTS_PER_REQUEST",
    "PREFIX",
    "PROVIDER",
    "REGION_KEY",
    "ROWS_PER_PASS",
    "SERVICE_NAME_KEY",
    "SETTLE_SECONDS",
    "WATERMARK_KEY",
    "Destination",
    "ExportConfig",
    "MetricsExport",
    "NewRelic",
    "Outcome",
    "Pass",
    "Sent",
    "Status",
    "StoredExport",
    "batches",
    "counters",
    "export_service",
    "forget_key",
    "load_export",
    "points_in",
    "read_key",
    "read_watermark",
    "resolve",
    "store_export",
    "store_off",
    "stored_export",
    "whole_buckets",
    "write_watermark",
]
