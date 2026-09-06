"""Downloading a spec document, with the rails spec §5.1 asks for.

The operator gives the gateway a URL they found somewhere, and it fetches that
URL on a schedule with a credential attached. That is enough moving parts to be
worth being careful about, so this module holds four rules rather than one
function:

*A body has a ceiling.* ``http.max_response_bytes`` is checked against
``Content-Length`` first and then against the bytes as they arrive, so a spec URL
that turns out to serve a video is abandoned early rather than after it has been
read into memory.

*Redirects are followed by hand.* Up to :data:`MAX_REDIRECTS` hops, and the
credential stops being sent the moment the chain leaves the origin it started
on. httpx's own redirect handling strips ``Authorization``, but it cannot know
that an ``api_key`` credential's header — named by the upstream, so it could be
anything — is a secret too, and would forward it to whoever the redirect names.

*A content type is a hint, not an answer.* Plenty of servers hand out YAML as
``text/plain``, or JSON as ``application/octet-stream``. The bytes are sniffed:
JSON first, then YAML through ``safe_load``.

*Every failure has a type.* A network error, an HTTP status, an oversize body and
an unreadable document are four different things for the operator to do
something about, and the status in particular has to survive to the UI: a 401 on
a spec URL is the signal to configure spec credentials, not a generic failure.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Final, Literal, TypeAlias

import httpx
import yaml

from mcp_gateway.config import HttpSettings
from mcp_gateway.crypto import Credential
from mcp_gateway.openapi.diagnostics import SpecError
from mcp_gateway.outbound import credential_headers, origin_of, outbound_client, same_origin

logger = logging.getLogger(__name__)

#: Hops followed before giving up (spec §5.1).
MAX_REDIRECTS: Final = 5

#: What we would like, in the order we would like it. Servers routinely ignore
#: this, which is why the response is sniffed rather than trusted.
ACCEPT: Final = "application/json, application/yaml, text/yaml;q=0.9, */*;q=0.8"

#: How much of a failing response is quoted back to the operator. Upstreams put
#: the reason in the body — "token expired", "unknown API version" — often
#: enough to be worth showing.
ERROR_SNIPPET_BYTES: Final = 2048
ERROR_SNIPPET_CHARS: Final = 400

SUPPORTED_SCHEMES: Final = frozenset({"http", "https"})

#: A byte-order mark is legal at the start of the file and illegal in the
#: JSON grammar, so it is taken off before anything tries to parse it.
BOM: Final = chr(0xFEFF)

#: Which of the two syntaxes the document turned out to be written in.
ParsedAs: TypeAlias = Literal["json", "yaml"]


class SpecFetchError(SpecError):
    """A spec document could not be obtained.

    Every failure below is one of these, so a caller that only wants to report
    the problem has one thing to catch — and this is itself a
    :class:`~mcp_gateway.openapi.diagnostics.SpecError`, so a caller that wants
    the whole ingestion pipeline can catch that one instead. ``url`` is the URL
    that failed, which after a redirect is not necessarily the one the operator
    typed; ``status_code`` is set only when an HTTP response was the problem.
    """

    status_code: int | None = None

    def __init__(self, message: str, *, url: str) -> None:
        self.url = url
        super().__init__(message)


class SpecNetworkError(SpecFetchError):
    """The request never produced a response: DNS, TLS, connection, timeout."""

    def __init__(self, url: str, *, reason: str) -> None:
        self.reason = reason
        super().__init__(f"Could not reach {url}: {reason}", url=url)


class SpecStatusError(SpecFetchError):
    """The server answered, and the answer was not a document.

    Keeps the status because the operator's next move depends on it: 401 and 403
    mean "configure spec credentials", 404 means "check the URL".
    """

    def __init__(self, url: str, *, status_code: int, detail: str | None = None) -> None:
        self.status_code = status_code
        self.detail = detail
        described = f"HTTP {status_code} {httpx.codes.get_reason_phrase(status_code)}".rstrip()
        super().__init__(
            f"Fetching the spec from {url} returned {described}."
            + (f" {detail}" if detail else ""),
            url=url,
        )

    @property
    def needs_credentials(self) -> bool:
        """Whether this is the status that spec authentication exists for."""
        return self.status_code in (401, 403)


class SpecTooLargeError(SpecFetchError):
    """The document is larger than ``http.max_response_bytes`` allows."""

    def __init__(self, url: str, *, limit_bytes: int, declared_bytes: int | None = None) -> None:
        self.limit_bytes = limit_bytes
        self.declared_bytes = declared_bytes
        size = "is" if declared_bytes is None else f"is {declared_bytes} bytes,"
        super().__init__(
            f"The document at {url} {size} larger than the {limit_bytes}-byte limit "
            f"(http.max_response_bytes).",
            url=url,
        )


class SpecParseError(SpecFetchError):
    """Bytes arrived, and they are not an OpenAPI or Swagger document."""

    def __init__(self, url: str, *, reason: str) -> None:
        self.reason = reason
        super().__init__(f"The document at {url} could not be read: {reason}.", url=url)


class SpecRedirectError(SpecFetchError):
    """The redirect chain outlasted :data:`MAX_REDIRECTS`."""

    def __init__(self, url: str, *, hops: int) -> None:
        self.hops = hops
        super().__init__(
            f"The spec URL redirected more than {hops} times, reaching {url}.", url=url
        )


@dataclass(frozen=True, slots=True)
class FetchedSpec:
    """One downloaded document, before anything has been made of its contents."""

    #: The parsed document, in whatever shape the upstream serves it. Turning
    #: that into something uniform is tasks 009-012.
    document: dict[str, Any]
    #: Where the document actually came from, after any redirects.
    url: str
    #: The URL that was asked for.
    requested_url: str
    parsed_as: ParsedAs
    size_bytes: int
    #: As served — recorded for diagnosis, never trusted for parsing.
    content_type: str | None = None

    @property
    def redirected(self) -> bool:
        return self.url != self.requested_url


@dataclass(frozen=True, slots=True)
class _Redirect:
    target: httpx.URL


@dataclass(frozen=True, slots=True)
class _Document:
    body: bytes
    content_type: str | None


#: What one request produced: somewhere else to look, or the document itself.
_Hop: TypeAlias = _Redirect | _Document


async def fetch_spec(
    url: str | httpx.URL,
    *,
    credential: Credential | None = None,
    http: HttpSettings | None = None,
    client: httpx.AsyncClient | None = None,
) -> FetchedSpec:
    """Download and parse the spec document at ``url``.

    ``credential`` is whatever the server's ``spec_auth_mode`` resolved to. The
    three modes live in :func:`mcp_gateway.db.repo.spec_credential_for`, so this
    function only ever sees "a credential, or none" — and the wizard, which has
    no saved server to resolve, can pass one it was handed on the form.

    Pass ``client`` to reuse a connection pool; without one, a client is built
    for the call and closed after it. Either way the configured timeout is
    applied to the request itself, so a caller's client cannot widen it.

    Raises one of the :class:`SpecFetchError` subclasses; nothing else escapes.
    """
    limits = HttpSettings() if http is None else http
    if client is not None:
        return await _fetch(client, url, credential=credential, http=limits)
    async with outbound_client(limits) as owned:
        return await _fetch(owned, url, credential=credential, http=limits)


async def _fetch(
    client: httpx.AsyncClient,
    requested: str | httpx.URL,
    *,
    credential: Credential | None,
    http: HttpSettings,
) -> FetchedSpec:
    start = _supported(requested)
    timeout = httpx.Timeout(http.timeout_seconds)
    current = start
    send_credential = credential is not None

    for _ in range(MAX_REDIRECTS + 1):
        headers = {"Accept": ACCEPT}
        if send_credential:
            headers |= credential_headers(credential)

        hop = await _one_hop(client, current, headers=headers, http=http, timeout=timeout)
        if isinstance(hop, _Document):
            document, parsed_as = _parse(hop.body, content_type=hop.content_type, url=str(current))
            return FetchedSpec(
                document=document,
                url=str(current),
                requested_url=str(start),
                parsed_as=parsed_as,
                size_bytes=len(hop.body),
                content_type=hop.content_type,
            )

        target = _supported(hop.target)
        if send_credential and not same_origin(target, start):
            logger.warning(
                "The spec URL redirected from %s to %s; credentials were not sent onward.",
                _label(start),
                _label(target),
            )
        # Sticky: a chain that has once left the starting origin does not get the
        # credential back by returning to it. A redirect that bounces through a
        # third party is not a reason to trust it with the token.
        send_credential = send_credential and same_origin(target, start)
        # Origins rather than whole URLs: a spec URL can carry a key in its query.
        logger.debug("Following spec redirect %s -> %s", _label(current), _label(target))
        current = target

    raise SpecRedirectError(str(current), hops=MAX_REDIRECTS)


async def _one_hop(
    client: httpx.AsyncClient,
    url: httpx.URL,
    *,
    headers: dict[str, str],
    http: HttpSettings,
    timeout: httpx.Timeout,
) -> _Hop:
    """One request: a redirect to follow, or the body that ends the chain."""
    try:
        async with client.stream(
            "GET", url, headers=headers, follow_redirects=False, timeout=timeout
        ) as response:
            if response.has_redirect_location:
                return _Redirect(url.join(response.headers["location"]))
            if not response.is_success:
                raise SpecStatusError(
                    str(url), status_code=response.status_code, detail=await _snippet(response)
                )
            body = await _read_capped(response, url=url, limit=http.max_response_bytes)
            return _Document(body, response.headers.get("content-type"))
    except (httpx.HTTPError, httpx.InvalidURL) as exc:
        # httpx.HTTPStatusError cannot arrive here: the status is read directly
        # rather than through raise_for_status, so everything caught is a
        # transport failure — connect, read, write, timeout, protocol.
        raise SpecNetworkError(str(url), reason=_reason(exc)) from exc


async def _read_capped(response: httpx.Response, *, url: httpx.URL, limit: int) -> bytes:
    """Read the body, stopping the moment it is too big to be a spec."""
    declared = response.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > limit:
        # The cheap case: the server said how big it is, so nothing is read.
        raise SpecTooLargeError(str(url), limit_bytes=limit, declared_bytes=int(declared))

    chunks: list[bytes] = []
    total = 0
    async for chunk in response.aiter_bytes():
        total += len(chunk)
        if total > limit:
            # Raising leaves the context manager, which closes the connection —
            # the rest of the body is never transferred.
            raise SpecTooLargeError(str(url), limit_bytes=limit)
        chunks.append(chunk)
    return b"".join(chunks)


async def _snippet(response: httpx.Response) -> str | None:
    """The first little of a failed response, flattened onto one line."""
    chunks: list[bytes] = []
    read = 0
    try:
        async for chunk in response.aiter_bytes():
            chunks.append(chunk)
            read += len(chunk)
            if read >= ERROR_SNIPPET_BYTES:
                break
    except httpx.HTTPError:
        # The status is the news. A body that fails to arrive does not change it.
        return None
    text = " ".join(b"".join(chunks).decode("utf-8", errors="replace").split())
    if not text:
        return None
    return text if len(text) <= ERROR_SNIPPET_CHARS else text[:ERROR_SNIPPET_CHARS] + "..."


def _parse(body: bytes, *, content_type: str | None, url: str) -> tuple[dict[str, Any], ParsedAs]:
    """Sniff the bytes as JSON, then as YAML, whatever the server called them."""
    if not body.strip():
        raise SpecParseError(url, reason="the response is empty")

    text = _decode(body, content_type=content_type, url=url)
    parsed_as: ParsedAs = "json"
    try:
        document: Any = json.loads(text)
    except ValueError:
        # Every JSON document is also YAML, so this fallback is about speed and
        # about a better message, not about capability.
        parsed_as = "yaml"
        try:
            document = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            reason = f"it is neither JSON nor YAML ({_oneline(exc)})"
            raise SpecParseError(url, reason=reason) from exc

    if not isinstance(document, dict):
        # What an HTML error page served with a 200 looks like from here.
        found = "nothing" if document is None else type(document).__name__
        raise SpecParseError(url, reason=f"the top level is {found}, not an object")
    return document, parsed_as


def _decode(body: bytes, *, content_type: str | None, url: str) -> str:
    charset = _charset(content_type) or "utf-8"
    try:
        text = body.decode(charset)
    except (LookupError, UnicodeDecodeError) as exc:
        raise SpecParseError(url, reason=f"it is not text in {charset}") from exc
    return text.lstrip(BOM)


def _charset(content_type: str | None) -> str | None:
    """The charset a content type declares, if it declares one."""
    if not content_type:
        return None
    for part in content_type.split(";")[1:]:
        name, _, value = part.partition("=")
        if name.strip().lower() == "charset":
            return value.strip().strip('"') or None
    return None


def _supported(url: str | httpx.URL) -> httpx.URL:
    """Accept ``url`` only if it is something the gateway is willing to fetch.

    Applied to the operator's URL and to every redirect target, so neither a
    typed ``file://`` path nor a redirect to one reaches the transport.
    """
    try:
        parsed = httpx.URL(url)
    except httpx.InvalidURL as exc:
        raise SpecNetworkError(str(url), reason=_reason(exc)) from exc
    if parsed.scheme not in SUPPORTED_SCHEMES:
        raise SpecNetworkError(str(url), reason="only http:// and https:// URLs can be fetched")
    if not parsed.host:
        raise SpecNetworkError(str(url), reason="the URL names no host")
    return parsed


def _label(url: httpx.URL) -> str:
    """An origin, for a log line that must not carry a query string."""
    scheme, host, port = origin_of(url)
    return f"{scheme}://{host}:{port}"


def _reason(exc: Exception) -> str:
    return _oneline(exc) or type(exc).__name__


def _oneline(exc: Exception) -> str:
    return " ".join(str(exc).split())


__all__ = [
    "ACCEPT",
    "MAX_REDIRECTS",
    "FetchedSpec",
    "ParsedAs",
    "SpecFetchError",
    "SpecNetworkError",
    "SpecParseError",
    "SpecRedirectError",
    "SpecStatusError",
    "SpecTooLargeError",
    "fetch_spec",
]
