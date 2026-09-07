"""One spec URL in, one set of operations out (spec §5.1 to §5.3).

Reading a spec is five stages across four modules — fetch, convert, resolve,
normalise, extract — and every caller that reads one runs all five, in the same
order, for the same reasons. The add-server wizard does it, the JSON API's
``/specs/preview`` does it, and so does every refresh. If each of them wired the
stages up for itself, "what the gateway makes of this document" would have three
definitions, and the day they drifted apart would be the day a refresh started
finding changes that were never there.

So the order lives here, once, and the stages stay separate modules: each is
worth testing on its own against a document that is wrong in exactly one way,
and none of them needs to know what runs after it.

**Nothing here writes anything.** A :class:`SpecPreview` is a value. Storing one
is a decision the caller makes later, which is what lets the wizard show an
operator every operation they are about to register before a row exists (spec
§5.1) — and what lets a refresh compare a fresh reading against the stored one
without having committed to it.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

import httpx

from mcp_gateway.config import HttpSettings
from mcp_gateway.crypto import Credential
from mcp_gateway.openapi.diagnostics import SpecWarning
from mcp_gateway.openapi.fetch import fetch_spec
from mcp_gateway.openapi.normalize import normalize_document
from mcp_gateway.openapi.refs import resolve_refs
from mcp_gateway.openapi.schema import NormalizedOperation, extract_operations, schema_hash
from mcp_gateway.openapi.swagger2 import convert_to_openapi3
from mcp_gateway.outbound import credential_header_names

if TYPE_CHECKING:  # pragma: no cover - a type alias, not a dependency on the database
    from mcp_gateway.db.models import SpecFormat

logger = logging.getLogger(__name__)

#: Values a server variable may declare as its default. A YAML port number
#: arrives as an int, and a URL is built out of text either way.
SCALARS: Final = (str, int, float)


@dataclass(frozen=True, slots=True)
class SpecPreview:
    """Everything one document turned out to contain, and nothing stored.

    The fields line up with the columns a server row would get (spec §4) on
    purpose: this is what step 2 of the wizard, and the JSON API's create, save
    without having to read the document a second time.
    """

    #: The URL that was asked for.
    requested_url: str
    #: Where the document actually came from, after any redirects.
    fetched_url: str
    spec_format: SpecFormat
    #: ``info.title``, which is the wizard's default display name.
    title: str | None
    #: ``info.version`` — shown, never acted on. The gateway's own idea of
    #: whether a spec has changed is :attr:`spec_hash`, because a document that
    #: changes without its version changing is the common case.
    version: str | None
    #: Where the operations' paths hang off, or ``None`` when the document
    #: declines to say and the operator has to.
    base_url: str | None
    operations: tuple[NormalizedOperation, ...]
    #: Everything the four stages had to degrade, in the order they ran.
    warnings: tuple[SpecWarning, ...]
    #: sha256 of :attr:`document`; what a refresh compares (spec §5.4).
    spec_hash: str
    #: The normalised document, which is what the row's ``spec_snapshot``
    #: stores — the shape a later diff has to be able to compare against.
    document: dict[str, Any]

    @property
    def redirected(self) -> bool:
        return self.fetched_url != self.requested_url

    @property
    def operation_count(self) -> int:
        return len(self.operations)


def spec_hash(document: Mapping[str, Any]) -> str:
    """A digest of a whole normalised document (spec §4).

    The same canonical rendering a schema gets, for the same reason: a document
    that merely reordered its keys must not read as a change, or every refresh
    of a spec served from a generator that walks a dict would report one.
    """
    return schema_hash(document)


def base_url_of(document: Mapping[str, Any], source_url: str | None = None) -> str | None:
    """Where the API this document describes actually lives.

    The first usable ``servers`` entry, its variables filled in with the
    defaults the document declares, resolved against ``source_url``: a
    ``url: /v2`` is legal OpenAPI and means "the same host as this spec", which
    only the fetch URL can answer.

    Swagger 2's ``host`` + ``basePath`` + ``schemes`` has already become
    ``servers`` by the time this runs (task 010), so there is one shape to read
    rather than two.
    """
    servers = document.get("servers")
    if not isinstance(servers, list):
        return None
    for entry in servers:
        if not isinstance(entry, Mapping):
            continue
        declared = entry.get("url")
        if not isinstance(declared, str) or not declared.strip():
            continue
        return _absolute(_expand(declared.strip(), entry.get("variables")), source_url)
    return None


def read_spec(
    document: Mapping[str, Any],
    *,
    source_url: str,
    requested_url: str | None = None,
    api_credential: Credential | None = None,
) -> SpecPreview:
    """Run one already-downloaded document through every stage of ingestion.

    Separate from :func:`preview_spec` because the stages are pure and the fetch
    is not: a test, and a caller that already has the bytes, should be able to
    ask what the gateway makes of a document without a network in the picture.

    ``api_credential`` is never sent anywhere by this function. Only the *names*
    of the headers it occupies are used, so that a header parameter the gateway
    fills in itself is left out of the schemas a model is shown (spec §5.3).

    Raises :class:`~mcp_gateway.openapi.diagnostics.SpecError` — an unreadable
    version, a ``$ref`` into thin air — so one ``except`` covers the whole of
    ingestion.
    """
    converted = convert_to_openapi3(document, source_url=source_url)
    resolved = resolve_refs(converted.document)
    normalized = normalize_document(resolved.document)
    extracted = extract_operations(
        normalized.document, supplied_headers=credential_header_names(api_credential)
    )

    info = normalized.document.get("info")
    fields = info if isinstance(info, Mapping) else {}
    return SpecPreview(
        requested_url=requested_url if requested_url is not None else source_url,
        fetched_url=source_url,
        spec_format=converted.source_format,
        title=_text(fields.get("title")),
        version=_text(fields.get("version")),
        base_url=base_url_of(normalized.document, source_url),
        operations=extracted.operations,
        # In the order the stages ran, so the first warning an operator reads is
        # about the earliest thing that went wrong.
        warnings=(
            converted.warnings + resolved.warnings + normalized.warnings + extracted.warnings
        ),
        spec_hash=spec_hash(normalized.document),
        document=normalized.document,
    )


async def preview_spec(
    url: str,
    *,
    spec_credential: Credential | None = None,
    api_credential: Credential | None = None,
    http: HttpSettings | None = None,
    client: httpx.AsyncClient | None = None,
) -> SpecPreview:
    """Fetch the document at ``url`` and read it, without storing a thing.

    The two credentials do different jobs and are deliberately named apart:
    ``spec_credential`` is what the download itself is made with (spec §5.1),
    and ``api_credential`` is the one the *API* uses, which is consulted only
    for the header names it occupies.

    Raises :class:`~mcp_gateway.openapi.fetch.SpecFetchError` for anything that
    went wrong getting the document and
    :class:`~mcp_gateway.openapi.diagnostics.SpecError` for anything wrong with
    it once it arrived; the first is a subclass of the second.
    """
    fetched = await fetch_spec(url, credential=spec_credential, http=http, client=client)
    preview = read_spec(
        fetched.document,
        source_url=fetched.url,
        requested_url=fetched.requested_url,
        api_credential=api_credential,
    )
    logger.info(
        "Read %d operation(s) from %s (%s, %d warning(s))",
        preview.operation_count,
        preview.requested_url,
        preview.spec_format,
        len(preview.warnings),
    )
    return preview


def _expand(url: str, variables: Any) -> str:
    """Fill ``{region}`` placeholders from the server variables' defaults.

    A variable with no usable default is left as it was written. A URL with a
    brace still in it is visibly wrong, which is a better thing to hand an
    operator than a URL that is quietly wrong.
    """
    if not isinstance(variables, Mapping):
        return url
    for name, definition in variables.items():
        if not isinstance(definition, Mapping):
            continue
        default = definition.get("default")
        if isinstance(default, SCALARS) and not isinstance(default, bool):
            url = url.replace(f"{{{name}}}", str(default))
    return url


def _absolute(url: str, source_url: str | None) -> str:
    """``url`` made absolute against where the document came from."""
    if source_url:
        try:
            url = str(httpx.URL(source_url).join(url))
        except httpx.InvalidURL:
            # A caller that passed something that is not a URL still gets the
            # document's own answer, which is the more useful half anyway.
            logger.debug("Could not resolve %r against %r", url, source_url)
    # Trailing slashes are noise here: the proxy joins a path onto this and
    # strips one either way, and two servers that differ only by one would look
    # like two different upstreams on the page.
    return url.rstrip("/") or url


def _text(value: Any) -> str | None:
    """A stripped string, or ``None`` for anything that says nothing.

    Stringified rather than type-checked: a YAML ``version: 1.0`` is a float,
    and it is still the version the document meant.
    """
    if value is None or isinstance(value, (list, dict)):
        return None
    text = str(value).strip()
    return text or None


__all__ = [
    "SpecPreview",
    "base_url_of",
    "preview_spec",
    "read_spec",
    "spec_hash",
]
