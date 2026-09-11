"""Adding a server, step 1: the form, and what it is worth before it is saved.

Spec §5.1 and §7.1, task 021. The operator gives the gateway a spec URL and, if
the upstream wants them, credentials; the gateway fetches, parses and reports,
and writes nothing at all. Only step 2 (task 022) saves.

Three things live here rather than in the routes, because each is a rule rather
than a request:

**Two credentials, four shapes each.** One authenticates the API, the other the
spec download, and the second has a mode that says whether it exists at all
(spec §5.1). Turning fifteen form fields into two
:class:`~mcp_gateway.crypto.Credential` objects is where a form can be wrong in
a dozen ways, and every one of them has to come back as a sentence beside the
field that caused it — all of them at once, not the first.

**Nothing secret is ever echoed.** A re-rendered form carries back everything
the operator typed except the credentials: :func:`kept_fields` is the whole list
of what may go in a ``value`` attribute, and it is a list of names rather than a
list of exclusions, so a field added later is left out until somebody decides
otherwise. It costs a retype after a mistake. That is the right trade at exactly
the moment it matters — the resubmission after a 401 is the case where keeping
the credential would mean keeping the wrong one.

**A preview waits in memory, briefly.** Step 2 has to be able to save the
credentials the operator typed on step 1, and it cannot be handed them through
the browser without rendering them. So a successful preview is held in
:class:`PreviewStore` under an unguessable token, in this process only, for half
an hour. A *failed* one is not held at all: a store of rejected credentials is a
store of secrets nobody asked us to keep.

**An MCP server's step 1 is the same form with fewer questions** (task 133).
An endpoint is one thing with one credential (spec §4), so the form asks for
the endpoint, a display name, and how to authenticate to it — no base URL
override, no separate spec credential, because those are questions with no
answer here. :func:`parse_mcp_form` reads it into the same :class:`WizardForm`
the API form produces, with the endpoint in ``spec_url`` and the mode already
``same_as_api``, which is what lets step 2 and the save be one piece of code
for both kinds. A ``401`` or ``403`` on connecting lands beside the
authentication selector, as the spec URL's does beside the spec-auth one.
"""

from __future__ import annotations

import logging
import secrets
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final, Literal, TypeVar

import httpx

from mcp_gateway.crypto import (
    ApiKeyCredential,
    BasicCredential,
    BearerCredential,
    Credential,
    CredentialType,
    HeadersCredential,
)
from mcp_gateway.db.models import AuthType, SpecAuthMode
from mcp_gateway.db.repo import KIND_MCP, KIND_OPENAPI
from mcp_gateway.mcpclient.connect import EndpointError, EndpointStatusError
from mcp_gateway.mcpclient.preview import EndpointPreview
from mcp_gateway.openapi.diagnostics import SpecError, SpecWarning
from mcp_gateway.openapi.fetch import SpecStatusError
from mcp_gateway.openapi.ingest import SpecPreview
from mcp_gateway.openapi.schema import NormalizedOperation

logger = logging.getLogger(__name__)

#: One of the string literal sets below.
Choice = TypeVar("Choice", bound=str)

#: The API authentication schemes the form offers, in the order it offers them.
AUTH_TYPES: Final[tuple[AuthType, ...]] = ("none", "bearer", "api_key", "basic", "headers")

#: The same four minus ``none``: a spec credential exists or the mode says it
#: does not, so "no credential" is not one of its shapes.
CREDENTIAL_TYPES: Final[tuple[CredentialType, ...]] = ("bearer", "api_key", "basic", "headers")

SPEC_AUTH_MODES: Final[tuple[SpecAuthMode, ...]] = ("none", "same_as_api", "custom")

#: What a re-rendered form is allowed to put back in a ``value`` attribute.
#: Named rather than derived, so adding a field does not silently add it here.
KEPT: Final = ("spec_url", "name", "base_url", "auth_type", "spec_auth_mode", "spec_auth_type")

#: The MCP section's step 1 (task 133): its URL box, and the two fields it
#: shares with the API form. The same rule as :data:`KEPT`, for the same
#: reason — a list of names, none of them a credential.
ENDPOINT_FIELD: Final = "endpoint"
MCP_KEPT: Final = (ENDPOINT_FIELD, "name", "auth_type")

#: The only two schemes a spec URL or a base URL may use. ``file://`` is refused
#: at the form rather than at the transport so the operator is told why.
SCHEMES: Final = ("http://", "https://")

URL_REQUIRED: Final = "Enter the URL of the OpenAPI or Swagger document."
URL_SCHEME: Final = "The URL has to start with http:// or https://."
BASE_URL_SCHEME: Final = "The base URL has to start with http:// or https://."
#: Said by the form and by the JSON API, so it names no direction: "above" is
#: true of one of them and meaningless to the other.
NOTHING_TO_REUSE: Final = (
    "There is no API credential to reuse. Choose an API authentication type, "
    "or authenticate the spec URL separately."
)
HEADER_LINE: Final = "Write one header per line, as 'Name: value'."

#: Added to the message when the spec URL itself refused the request, which is
#: the whole reason this form has a second credential on it.
SPEC_AUTH_HINT: Final = "The spec URL needs credentials of its own to be downloaded."

ENDPOINT_REQUIRED: Final = "Enter the URL of the MCP endpoint."
#: Added to the message when the endpoint refused the connection, beside the
#: one selector on the form that can fix it (task 133). Said by the JSON API
#: and by a tool result too (task 134), so it names no form.
ENDPOINT_AUTH_HINT: Final = "This server needs a credential it was not given."

#: Said beside a display name the operator did not type. Step 1 promised the
#: document would supply one, and step 2 is where that promise is kept or not,
#: so the page that shows the name says which of the two it got (task 115).
NAME_FROM_DOCUMENT: Final = "From the document"
NAME_FROM_URL: Final = "From the spec URL"
#: The same two for an MCP server, whose name comes from ``initialize`` when
#: the operator did not type one, and from the endpoint's host when the
#: upstream did not say either (spec §5b.2).
NAME_FROM_SERVER: Final = "From the server itself"
NAME_FROM_ENDPOINT: Final = "From the endpoint URL"

#: How long a previewed spec waits for the operator to work through step 2.
#: Long enough to read a hundred operations and decide; short enough that a
#: browser tab left open over a weekend is not still holding a token.
PREVIEW_TTL: Final = 30 * 60

#: How many previews are held at once. The oldest goes when a new one arrives:
#: this is one operator adding one server at a time, and an unbounded cache of
#: credentials is not something to leave lying about because it was easy.
MAX_PENDING: Final = 20


# Spelled as a state rather than as an error, like the exceptions in ``crypto``:
# it reads as the condition a caller is reacting to.
class FormInvalid(Exception):  # noqa: N818
    """One or more fields the operator has to put right.

    Carries every problem found, keyed by the field it belongs to, because a
    form that reports its faults one at a time makes the operator submit it
    once per mistake.
    """

    def __init__(self, errors: Mapping[str, str]) -> None:
        self.errors = dict(errors)
        super().__init__("; ".join(f"{name}: {why}" for name, why in self.errors.items()))


@dataclass(frozen=True, slots=True)
class WizardForm:
    """Step 1 as the operator filled it in, once it has been understood.

    The credentials are parsed models rather than strings: by the time a form
    is one of these, "is this a usable credential" has already been answered.
    """

    spec_url: str
    name: str = ""
    #: An override. Empty means "whatever the document says".
    base_url: str = ""
    auth_type: AuthType = "none"
    spec_auth_mode: SpecAuthMode = "none"
    #: Only meaningful when :attr:`spec_auth_mode` is ``custom``; the form still
    #: remembers it so a re-render puts the selector back where it was.
    spec_auth_type: CredentialType = "bearer"
    credential: Credential | None = None
    spec_credential: Credential | None = None

    @property
    def fetch_credential(self) -> Credential | None:
        """Which credential the spec download itself is made with (spec §5.1).

        The three modes, in one place, so the wizard and
        :func:`mcp_gateway.db.repo.spec_credential_for` — which answers the same
        question for a server that exists — cannot mean different things by
        ``same_as_api``.
        """
        if self.spec_auth_mode == "none":
            return None
        if self.spec_auth_mode == "same_as_api":
            return self.credential
        return self.spec_credential


@dataclass(frozen=True, slots=True)
class PendingServer:
    """A previewed upstream and the form it came from, waiting for step 2.

    The properties are the defaults step 1 promised: an operator who left the
    name or the base URL blank was told the upstream would supply them, and
    this is where that is decided — once, so the preview page and the save
    cannot show and store different things.

    The preview is of either kind (spec §5b.2). Everything step 2 does — the
    table, the names, the save — works off :attr:`operations`, which both
    previews produce in the same record, and off the four answers below, which
    are the only places the two kinds are told apart.
    """

    form: WizardForm
    preview: SpecPreview | EndpointPreview

    @property
    def kind(self) -> Literal["openapi", "mcp"]:
        """What kind of row the save will write: decided by what was read."""
        return KIND_MCP if isinstance(self.preview, EndpointPreview) else KIND_OPENAPI

    @property
    def name(self) -> str:
        """What the server will be called: the operator, then the upstream."""
        return self.form.name or self._offered_name or _host_of(self.form.spec_url)

    @property
    def base_url(self) -> str | None:
        """Where tool calls will go, or ``None`` if nobody has said yet.

        For an MCP server, always the endpoint: a listing and a call go to
        one place, and the form has no box to say otherwise (spec §4).
        """
        if isinstance(self.preview, EndpointPreview):
            return self.preview.url
        return self.form.base_url or self.preview.base_url

    @property
    def name_note(self) -> str | None:
        """Where :attr:`name` came from, when it did not come from the form.

        ``None`` for a name the operator typed, which needs no explanation.
        Answered here rather than in a template because it is the other half of
        :attr:`name`, and two places deciding where a name came from is two
        places that can disagree about it.
        """
        if self.form.name:
            return None
        if isinstance(self.preview, EndpointPreview):
            return NAME_FROM_SERVER if self._offered_name else NAME_FROM_ENDPOINT
        return NAME_FROM_DOCUMENT if self._offered_name else NAME_FROM_URL

    @property
    def operations(self) -> tuple[NormalizedOperation, ...]:
        return self.preview.operations

    @property
    def warnings(self) -> tuple[SpecWarning, ...]:
        """What ingestion had to degrade. Nothing, for a tool list taken as sent."""
        if isinstance(self.preview, EndpointPreview):
            return ()
        return self.preview.warnings

    @property
    def _offered_name(self) -> str | None:
        """The name the upstream gave itself: ``info.title``, or ``serverInfo``."""
        if isinstance(self.preview, EndpointPreview):
            return self.preview.display_name
        return self.preview.title


def kept_fields(fields: Mapping[str, str]) -> dict[str, str]:
    """What a re-rendered form may show back to the operator.

    The one chokepoint between a submitted form and a rendered one. Everything
    not named in :data:`KEPT` — which is every credential field — comes back
    blank, whether or not the template remembered to leave it out.
    """
    return {name: str(fields.get(name, "")).strip() for name in KEPT}


def mcp_kept_fields(fields: Mapping[str, str]) -> dict[str, str]:
    """The same chokepoint for the MCP section's step 1 (task 133)."""
    return {name: str(fields.get(name, "")).strip() for name in MCP_KEPT}


def mcp_form_fields(form: WizardForm) -> dict[str, str]:
    """A parsed MCP step-1 form back as the mapping its template takes.

    What **Back** on step 2 needs, as :func:`form_fields` is for the API form.
    The endpoint lives in ``spec_url`` on the form and in the ``endpoint`` box
    on the page, so this is where the one is put back into the other.
    """
    return mcp_kept_fields(
        {ENDPOINT_FIELD: form.spec_url, "name": form.name, "auth_type": form.auth_type}
    )


def form_fields(form: WizardForm) -> dict[str, str]:
    """A parsed step-1 form back as the mapping its template takes.

    What **Back** on step 2 needs: the preview holds the form, and the operator
    who went back to correct one field should not retype the other five.

    Through :func:`kept_fields` like every other route into that template, so
    that this door is no wider than the others. It cannot widen: the two
    credentials are :class:`~mcp_gateway.crypto.Credential` models rather than
    strings, :data:`KEPT` names only the six fields that are strings, and
    ``WizardForm.credential`` and ``WizardForm.spec_credential`` never reach a
    template.
    """
    return kept_fields({name: str(getattr(form, name)) for name in KEPT})


def parse_form(fields: Mapping[str, str]) -> WizardForm:
    """Read a submitted step-1 form, or raise with every fault it has.

    The URL checks are made here as well as in the fetch, on purpose: a typo
    caught by the form is a red field, and the same typo caught by the
    transport is a paragraph about DNS.
    """
    errors: dict[str, str] = {}

    spec_url = _clean(fields.get("spec_url"))
    if not spec_url:
        errors["spec_url"] = URL_REQUIRED
    elif not spec_url.lower().startswith(SCHEMES):
        errors["spec_url"] = URL_SCHEME

    base_url = _clean(fields.get("base_url"))
    if base_url and not base_url.lower().startswith(SCHEMES):
        errors["base_url"] = BASE_URL_SCHEME

    auth_type = _one_of(fields.get("auth_type"), AUTH_TYPES, "none")
    if auth_type is None:
        errors["auth_type"] = "Choose one of the authentication types offered."
        auth_type = "none"
    credential = read_credential(fields, auth_type, prefix="", errors=errors)

    mode = _one_of(fields.get("spec_auth_mode"), SPEC_AUTH_MODES, "none")
    if mode is None:
        errors["spec_auth_mode"] = "Choose one of the spec authentication modes offered."
        mode = "none"

    spec_auth_type = _one_of(fields.get("spec_auth_type"), CREDENTIAL_TYPES, "bearer")
    if spec_auth_type is None:
        errors["spec_auth_type"] = "Choose one of the authentication types offered."
        spec_auth_type = "bearer"

    spec_credential: Credential | None = None
    if mode == "same_as_api" and auth_type == "none":
        # Reusing a credential there is none of is a form that says two
        # different things, not a spec fetched anonymously.
        errors["spec_auth_mode"] = NOTHING_TO_REUSE
    elif mode == "custom":
        spec_credential = read_credential(fields, spec_auth_type, prefix="spec_", errors=errors)

    if errors:
        raise FormInvalid(errors)
    return WizardForm(
        spec_url=spec_url,
        name=_clean(fields.get("name")),
        base_url=base_url,
        auth_type=auth_type,
        spec_auth_mode=mode,
        spec_auth_type=spec_auth_type,
        credential=credential,
        spec_credential=spec_credential,
    )


def parse_mcp_form(fields: Mapping[str, str]) -> WizardForm:
    """Read the MCP section's step 1, or raise with every fault it has (task 133).

    The result is a :class:`WizardForm` like the API form's, because step 2
    and the save read one shape: the endpoint is ``spec_url`` — it is where
    the tools are listed from, which is what that column means (spec §4) —
    there is no base URL override, and the spec-auth mode is ``same_as_api``,
    which is the one credential applied to everything.
    """
    errors: dict[str, str] = {}

    endpoint = _clean(fields.get(ENDPOINT_FIELD))
    if not endpoint:
        errors[ENDPOINT_FIELD] = ENDPOINT_REQUIRED
    elif not endpoint.lower().startswith(SCHEMES):
        errors[ENDPOINT_FIELD] = URL_SCHEME

    auth_type = _one_of(fields.get("auth_type"), AUTH_TYPES, "none")
    if auth_type is None:
        errors["auth_type"] = "Choose one of the authentication types offered."
        auth_type = "none"
    credential = read_credential(fields, auth_type, prefix="", errors=errors)

    if errors:
        raise FormInvalid(errors)
    return WizardForm(
        spec_url=endpoint,
        name=_clean(fields.get("name")),
        auth_type=auth_type,
        spec_auth_mode="same_as_api",
        credential=credential,
    )


def parse_headers(text: str) -> dict[str, str]:
    """``Name: value`` per line, for the credential that is a header map.

    A textarea rather than a pair of boxes because the upstreams that want this
    want two or three headers, and pasting them is how they arrive. Raises
    :class:`ValueError` naming the line that could not be read.
    """
    headers: dict[str, str] = {}
    for number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        name, separator, value = line.partition(":")
        if not separator or not name.strip():
            raise ValueError(f"Line {number} is not a header. {HEADER_LINE}")
        headers[name.strip()] = value.strip()
    if not headers:
        raise ValueError(HEADER_LINE)
    return headers


def failure_field(error: SpecError) -> str:
    """Which field an ingestion failure belongs beside.

    A 401 or a 403 on the spec URL is not a broken URL, it is a missing
    credential, and putting that message next to the URL box would send the
    operator off to check a URL that is perfectly correct (spec §5.1).
    """
    if isinstance(error, SpecStatusError) and error.needs_credentials:
        return "spec_auth_mode"
    return "spec_url"


def failure_message(error: SpecError) -> str:
    """What to say about a failed preview, upstream status and all."""
    message = str(error)
    if failure_field(error) == "spec_auth_mode":
        return f"{message} {SPEC_AUTH_HINT}"
    return message


def endpoint_failure_field(error: EndpointError) -> str:
    """Which field a failed connection belongs beside (task 133).

    A ``401`` or a ``403`` from the endpoint is a missing or wrong credential,
    and the selector that chooses one is where the operator fixes it; every
    other failure — could not connect, not an MCP server, refused the
    listing — is about the URL.
    """
    if isinstance(error, EndpointStatusError) and error.needs_credentials:
        return "auth_type"
    return ENDPOINT_FIELD


def endpoint_failure_message(error: EndpointError) -> str:
    """What to say about a failed connection, upstream status and all."""
    message = str(error)
    if endpoint_failure_field(error) == "auth_type":
        return f"{message} {ENDPOINT_AUTH_HINT}"
    return message


class PreviewStore:
    """Previewed specs waiting for step 2, in this process and nowhere else.

    In memory rather than in the database because the whole promise of step 1 is
    that nothing is written: a preview holds credentials the operator has typed
    and not yet decided to keep, and the store is emptied by a restart, which is
    the correct fate for all of them.

    A token is unguessable and short-lived, which is what stands in for binding
    it to a session — there is no session at all when the gateway runs open
    (spec §3.3), so a check against one would protect the configuration nobody
    chose to lock and nothing else.
    """

    __slots__ = ("_entries", "_max_entries", "_ttl")

    def __init__(self, *, ttl_seconds: float = PREVIEW_TTL, max_entries: int = MAX_PENDING) -> None:
        self._ttl = ttl_seconds
        self._max_entries = max_entries
        # Insertion-ordered, which is what makes "drop the oldest" one line.
        self._entries: dict[str, tuple[float, PendingServer]] = {}

    def __repr__(self) -> str:
        return f"{type(self).__name__}(pending={len(self)})"

    def __len__(self) -> int:
        self._sweep()
        return len(self._entries)

    def put(self, pending: PendingServer) -> str:
        """Hold ``pending`` and return the token that fetches it back."""
        self._sweep()
        token = secrets.token_urlsafe(24)
        self._entries[token] = (time.monotonic() + self._ttl, pending)
        while len(self._entries) > self._max_entries:
            dropped, _ = next(iter(self._entries.items()))
            del self._entries[dropped]
            logger.debug("Dropped the oldest pending server preview to stay within the cap")
        return token

    def get(self, token: str) -> PendingServer | None:
        """The preview a token names, or ``None`` if it has gone or expired."""
        entry = self._entries.get(token)
        if entry is None:
            return None
        expires_at, pending = entry
        if expires_at <= time.monotonic():
            del self._entries[token]
            return None
        return pending

    def pop(self, token: str) -> PendingServer | None:
        """The preview a token names, taken out of the store.

        What step 2 uses once it has saved: a preview that has become a server
        is a set of credentials with nothing left to do.
        """
        pending = self.get(token)
        if pending is not None:
            del self._entries[token]
        return pending

    def _sweep(self) -> None:
        now = time.monotonic()
        for token in [t for t, (expires_at, _) in self._entries.items() if expires_at <= now]:
            del self._entries[token]


def read_credential(
    fields: Mapping[str, str],
    auth_type: str,
    *,
    prefix: str,
    errors: dict[str, str],
) -> Credential | None:
    """Build one credential from the fields its shape uses.

    Faults are recorded against the field that carries them rather than raised,
    so one submission reports everything wrong with both credentials at once —
    and so the API credential's faults and the spec credential's arrive
    together, which is how an operator who got both wrong finds out.

    Public because the settings page (task 023) replaces a credential through
    the same four shapes and the same field names. Two readings of "what an API
    key is" is exactly the disagreement this saves.
    """

    def value(name: str) -> str:
        return _clean(fields.get(f"{prefix}{name}"))

    def need(name: str, label: str) -> str:
        found = value(name)
        if not found:
            errors[f"{prefix}{name}"] = f"{label} is needed for this authentication type."
        return found

    match auth_type:
        case "bearer":
            token = need("token", "A token")
            return BearerCredential(token=token) if token else None  # type: ignore[arg-type]
        case "api_key":
            header, key = need("header", "A header name"), need("value", "A header value")
            if not header or not key:
                return None
            return ApiKeyCredential(header=header, value=key)  # type: ignore[arg-type]
        case "basic":
            user, password = need("username", "A username"), need("password", "A password")
            if not user or not password:
                return None
            return BasicCredential(username=user, password=password)  # type: ignore[arg-type]
        case "headers":
            try:
                headers = parse_headers(fields.get(f"{prefix}headers", ""))
            except ValueError as exc:
                errors[f"{prefix}headers"] = str(exc)
                return None
            return HeadersCredential(headers=headers)  # type: ignore[arg-type]
    # ``none``, and anything the selector check already rejected.
    return None


def _clean(value: object) -> str:
    return str(value).strip() if isinstance(value, str) else ""


def _one_of(value: object, allowed: tuple[Choice, ...], default: Choice) -> Choice | None:
    """``value`` if the form offered it, ``default`` if it said nothing at all.

    ``None`` — which the caller turns into an error — is reserved for a value
    that was submitted and is not on the list, because that is a form nobody
    filled in through the page.
    """
    text = _clean(value)
    if not text:
        return default
    return text if text in allowed else None


def _host_of(url: str) -> str:
    """The host of a spec or endpoint URL, as the last resort for a display name."""
    try:
        return httpx.URL(url).host or url
    except httpx.InvalidURL:  # pragma: no cover - the form has already checked
        return url


#: How each authentication type is described on the form. One dictionary, so
#: the two selectors cannot end up calling the same thing different names.
AUTH_LABELS: Final[dict[str, str]] = {
    "none": "None",
    "bearer": "Bearer token",
    "api_key": "API key header",
    "basic": "Username and password",
    "headers": "Custom headers",
}

#: The spec-auth modes, in the words spec §5.1 uses for them.
MODE_LABELS: Final[dict[str, str]] = {
    "none": "Fetch the spec anonymously",
    "same_as_api": "Use the API credentials above",
    "custom": "Use a separate credential",
}


def options(values: tuple[str, ...], labels: Mapping[str, str]) -> list[tuple[str, str]]:
    """``(value, label)`` pairs for a selector, in the order they are offered."""
    return [(value, labels.get(value, value)) for value in values]


__all__ = [
    "AUTH_LABELS",
    "AUTH_TYPES",
    "BASE_URL_SCHEME",
    "CREDENTIAL_TYPES",
    "ENDPOINT_AUTH_HINT",
    "ENDPOINT_FIELD",
    "ENDPOINT_REQUIRED",
    "HEADER_LINE",
    "KEPT",
    "MAX_PENDING",
    "MCP_KEPT",
    "MODE_LABELS",
    "NAME_FROM_DOCUMENT",
    "NAME_FROM_ENDPOINT",
    "NAME_FROM_SERVER",
    "NAME_FROM_URL",
    "NOTHING_TO_REUSE",
    "PREVIEW_TTL",
    "SPEC_AUTH_HINT",
    "SPEC_AUTH_MODES",
    "URL_REQUIRED",
    "URL_SCHEME",
    "FormInvalid",
    "PendingServer",
    "PreviewStore",
    "WizardForm",
    "endpoint_failure_field",
    "endpoint_failure_message",
    "failure_field",
    "failure_message",
    "form_fields",
    "kept_fields",
    "mcp_form_fields",
    "mcp_kept_fields",
    "options",
    "parse_form",
    "parse_headers",
    "parse_mcp_form",
    "read_credential",
]
