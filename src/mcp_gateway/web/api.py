"""What the JSON API accepts and what it answers with (spec §7.3).

The pages and this API are two ways of asking for the same things, and the whole
point of the arrangement below is that they are two *interfaces* rather than two
implementations. Every write here runs through the same functions the forms run
through — :func:`mcp_gateway.web.picker.register` creates a server,
:func:`mcp_gateway.web.detail.apply_patch` edits one,
:func:`mcp_gateway.web.detail.apply_operation` edits an operation — so "creating
a server through the API produces the same state as the wizard does" is a
consequence of there being one implementation, not a promise anybody has to keep
in mind.

**Reads are the repository's own models.** ``GET /servers`` answers with
:class:`~mcp_gateway.db.repo.ServerSummary` and ``GET /servers/{id}`` with
:class:`~mcp_gateway.db.repo.ServerDetail`, unwrapped and unadapted. Those types
carry a credential's *state* — ``none`` / ``stored`` / ``missing`` — and its
mode, and have no field a credential value could be put in. Serialising them
directly is what makes "no response body ever contains a stored credential" a
property of the type rather than of each handler remembering (spec §7.3).

**A write says what it changes and nothing else.** ``PATCH /servers/{id}``
applies exactly the keys the body carried: a key left out keeps what is stored,
and a key set to ``null`` clears it. For an ordinary field that distinction is
convenience; for a credential it is the whole design, and it is the same one the
detail page's Replace checkbox makes — a credential nobody mentioned is a
credential nobody read, let alone overwrote.

**Every list is an object.** ``{"servers": [...]}`` rather than a bare array,
because a top-level JSON array is the one shape that cannot gain a field later
without breaking every caller, and both of these lists will want one.

**Two endpoints in spec §7.3 are not here yet.**
``POST /servers/{id}/refresh`` is the refresh engine (task 025) and
``GET /metrics`` is the metrics aggregation (task 029); both are milestones of
their own that build on this one. They are absent rather than stubbed: a route
that answers "not implemented" is a route a caller has to learn to distinguish
from one that works.
"""

from __future__ import annotations

import time
from typing import Any, Final

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator

from mcp_gateway import __version__
from mcp_gateway.config import Settings
from mcp_gateway.crypto import Credential
from mcp_gateway.db import repo
from mcp_gateway.db.models import SpecAuthMode
from mcp_gateway.naming import sanitize, server_slug
from mcp_gateway.openapi.diagnostics import SpecWarning
from mcp_gateway.openapi.ingest import SpecPreview
from mcp_gateway.web.picker import FALLBACK_SLUG
from mcp_gateway.web.wizard import (
    BASE_URL_SCHEME,
    NOTHING_TO_REUSE,
    SCHEMES,
    URL_REQUIRED,
    URL_SCHEME,
    PendingServer,
    WizardForm,
)

#: What a ``custom`` spec-auth mode is refused for. The detail page says the
#: same thing in the same words; there is one rule, stated once.
CUSTOM_NEEDS_CREDENTIAL: Final = (
    "A separate credential for the spec URL is needed when the mode is 'custom'."
)

#: What a create is refused for when it names operations the document does not
#: have. Silently registering the rest would give the caller a server with
#: fewer tools than they asked for and no way to find out.
UNKNOWN_SELECTION: Final = (
    "This document has no operation with the key {keys}. "
    "Ask POST /api/v1/specs/preview for the keys it does have."
)

#: How many unknown keys are named before the message gives up listing them.
MAX_UNKNOWN_SHOWN: Final = 5


# --------------------------------------------------------------------------- #
# Health
# --------------------------------------------------------------------------- #


class Health(BaseModel):
    """Body of ``GET /healthz`` and of ``GET /api/v1/health``.

    Served twice, from one model and one function: open at ``/healthz`` for a
    probe (spec §4) and behind the session at ``/api/v1/health`` because §7.3
    lists it there. Carries only what a probe or an operator needs; never a
    credential, and never anything that would help someone map the gateway's
    upstreams.
    """

    status: str = "ok"
    version: str
    uptime_seconds: float
    #: The config file in use, or ``None`` when the process runs on defaults.
    config_path: str | None = None


def health_report(settings: Settings, started_at: float | None) -> Health:
    """How the gateway is, right now."""
    return Health(
        version=__version__,
        # Zero rather than an error when the lifespan has not run: a probe asking
        # how long we have been up deserves a number, not a 500.
        uptime_seconds=round(0.0 if started_at is None else time.monotonic() - started_at, 3),
        config_path=str(settings.config_path) if settings.config_path else None,
    )


# --------------------------------------------------------------------------- #
# What comes back
# --------------------------------------------------------------------------- #


class ServerList(BaseModel):
    """``GET /servers``."""

    model_config = ConfigDict(frozen=True)

    servers: tuple[repo.ServerSummary, ...] = ()


class OperationList(BaseModel):
    """``GET /servers/{id}/operations``, narrowed by status if asked."""

    model_config = ConfigDict(frozen=True)

    operations: tuple[repo.OperationView, ...] = ()


class WarningOut(BaseModel):
    """One thing ingestion had to do differently than the document asked."""

    model_config = ConfigDict(frozen=True)

    code: str
    message: str
    location: str | None = None


class PreviewedOperation(BaseModel):
    """One operation a document declared, before anything is stored.

    ``input_schema`` is left out for the same reason
    :class:`~mcp_gateway.db.repo.OperationView` leaves it out: it is large, a
    preview of a two-hundred-operation document would be most of a megabyte of
    it, and nothing a caller does with this list needs it. ``op_key`` is what
    they need, since it is what ``selected`` on a create is written in.
    """

    model_config = ConfigDict(frozen=True)

    op_key: str
    operation_id: str | None
    method: str
    path: str
    summary: str | None
    description: str | None
    tags: tuple[str, ...] = ()
    input_schema_hash: str = ""


class SpecPreviewOut(BaseModel):
    """``POST /specs/preview``: everything a document turned out to contain.

    Deliberately not the document itself. The normalised spec is stored as a
    server's snapshot when one is created, and handing back a megabyte of JSON
    the caller already has a URL for is not a service to anybody.
    """

    model_config = ConfigDict(frozen=True)

    requested_url: str
    fetched_url: str
    redirected: bool
    spec_format: str
    title: str | None
    version: str | None
    #: Where the operations' paths hang off, or ``None`` when the document
    #: declines to say — which is a create this API will refuse until the
    #: caller supplies ``base_url`` themselves.
    base_url: str | None
    spec_hash: str
    operation_count: int
    operations: tuple[PreviewedOperation, ...] = ()
    warnings: tuple[WarningOut, ...] = ()


def previewed(preview: SpecPreview) -> SpecPreviewOut:
    """Dress a :class:`~mcp_gateway.openapi.ingest.SpecPreview` for the wire."""
    return SpecPreviewOut(
        requested_url=preview.requested_url,
        fetched_url=preview.fetched_url,
        redirected=preview.redirected,
        spec_format=preview.spec_format,
        title=preview.title,
        version=preview.version,
        base_url=preview.base_url,
        spec_hash=preview.spec_hash,
        operation_count=preview.operation_count,
        operations=tuple(
            PreviewedOperation(
                op_key=operation.op_key,
                operation_id=operation.operation_id,
                method=operation.method,
                path=operation.path,
                summary=operation.summary,
                description=operation.description,
                tags=operation.tags,
                input_schema_hash=operation.input_schema_hash,
            )
            for operation in preview.operations
        ),
        warnings=tuple(_warning(warning) for warning in preview.warnings),
    )


def _warning(warning: SpecWarning) -> WarningOut:
    return WarningOut(code=warning.code, message=warning.message, location=warning.location)


# --------------------------------------------------------------------------- #
# What goes in
# --------------------------------------------------------------------------- #


class _SpecRequest(BaseModel):
    """The half of a request that describes a document and how to fetch it.

    Shared by the create and by the preview, because they are the same question
    asked twice: the preview is the create up to the point where it would write.

    A credential arrives as the object it is — ``{"type": "bearer", "token":
    "..."}`` — rather than as a type beside a bag of fields, so an API key with
    no header name is a validation failure rather than a row that authenticates
    with nothing. The authentication *type* is read off the credential for the
    same reason: two fields that must agree are two fields that can disagree.
    """

    model_config = ConfigDict(extra="forbid")

    spec_url: str
    #: An override for where tool calls go. Empty means "whatever the document
    #: says", which is a document that has to say something.
    base_url: str = ""
    #: ``None`` for an upstream that wants no authentication.
    credential: Credential | None = None
    spec_auth_mode: SpecAuthMode = "none"
    #: Only read when :attr:`spec_auth_mode` is ``custom``. Validated even when
    #: it is left out, because being left out is exactly the fault worth
    #: catching: ``custom`` with nothing to fetch with.
    spec_credential: Credential | None = Field(default=None, validate_default=True)

    @field_validator("spec_url")
    @classmethod
    def _usable_url(cls, value: str) -> str:
        url = value.strip()
        if not url:
            raise ValueError(URL_REQUIRED)
        if not url.lower().startswith(SCHEMES):
            raise ValueError(URL_SCHEME)
        return url

    @field_validator("base_url")
    @classmethod
    def _usable_base_url(cls, value: str) -> str:
        url = value.strip()
        if url and not url.lower().startswith(SCHEMES):
            raise ValueError(BASE_URL_SCHEME)
        return url

    @field_validator("spec_auth_mode")
    @classmethod
    def _reuse_needs_something_to_reuse(cls, value: str, info: ValidationInfo) -> str:
        # ``credential`` is declared above this field, so it has already been
        # validated and is in ``info.data`` — which is what lets this fault land
        # on the mode rather than on the request as a whole.
        if value == "same_as_api" and info.data.get("credential") is None:
            raise ValueError(NOTHING_TO_REUSE)
        return value

    @field_validator("spec_credential")
    @classmethod
    def _custom_needs_one(cls, value: Credential | None, info: ValidationInfo) -> Credential | None:
        if info.data.get("spec_auth_mode") == "custom" and value is None:
            raise ValueError(CUSTOM_NEEDS_CREDENTIAL)
        return value

    def as_form(self, *, name: str = "") -> WizardForm:
        """The same object step 1 of the wizard produces.

        Which is the point: from here on the two paths are one path.
        """
        return WizardForm(
            spec_url=self.spec_url,
            name=name,
            base_url=self.base_url,
            auth_type="none" if self.credential is None else self.credential.type,
            spec_auth_mode=self.spec_auth_mode,
            spec_auth_type=(
                self.spec_credential.type if self.spec_credential is not None else "bearer"
            ),
            credential=self.credential,
            spec_credential=self.spec_credential,
        )


class SpecPreviewIn(_SpecRequest):
    """``POST /specs/preview``. Fetches and parses; writes nothing at all."""


class ServerCreate(_SpecRequest):
    """``POST /servers``: fetch the document, then register what it describes.

    One call rather than the wizard's two, because a script has no step 2 to
    spend time on. What it gives up is the chance to look before choosing, and
    ``POST /specs/preview`` is there for a caller that wants to.

    ``enabled`` and ``auto_refresh`` are deliberately absent. This create is the
    wizard's save, and the wizard's save does not set them; they are one
    ``PATCH`` away, and keeping the two paths identical is worth more than
    saving a round trip.
    """

    #: What the server is called. Empty takes the document's title, and failing
    #: that the host of the spec URL — the same fallback step 1 offers.
    name: str = ""
    #: What leads every tool name from this server. Empty derives one from the
    #: display name, which is what the wizard fills its box with.
    tool_prefix: str = ""
    #: The operations to expose, by ``op_key``. ``null`` — the default — means
    #: all of them, which is what the picker arrives showing. An empty list
    #: means none, which is a legal thing to want.
    selected: list[str] | None = None

    def prefix_for(self, pending: PendingServer) -> str:
        """The tool prefix this server gets, asked for or derived.

        Sanitised rather than refused, like the pages: a caller who sent
        ``"Pet Store"`` gets ``Pet_Store``, because that is a name being used as
        an identifier and mapping it is what they asked for.
        """
        return sanitize(self.tool_prefix.strip()) or server_slug(pending.name) or FALLBACK_SLUG

    def selection(self, pending: PendingServer) -> list[str]:
        """Which operations to expose. Raises :class:`ValueError` on a key the
        document does not have."""
        available = {operation.op_key for operation in pending.operations}
        if self.selected is None:
            return sorted(available)
        unknown = [key for key in self.selected if key not in available]
        if unknown:
            shown = ", ".join(repr(key) for key in unknown[:MAX_UNKNOWN_SHOWN])
            if len(unknown) > MAX_UNKNOWN_SHOWN:
                shown += f", and {len(unknown) - MAX_UNKNOWN_SHOWN} more"
            raise ValueError(UNKNOWN_SELECTION.format(keys=shown))
        return list(self.selected)


#: The fields of :class:`ServerUpdate` that :class:`~mcp_gateway.db.repo.ServerPatch`
#: has too. Named rather than derived, so a field added to one of them does not
#: silently start being written by the other.
_PATCHABLE: Final = frozenset(
    {
        "name",
        "slug",
        "tool_prefix",
        "base_url",
        "enabled",
        "auto_refresh",
        "credential",
        "spec_auth_mode",
        "spec_credential",
    }
)


class ServerUpdate(BaseModel):
    """``PATCH /servers/{id}``: the fields to change, and no others.

    Absent keeps, ``null`` clears. For ``credential`` and ``spec_credential``
    that is the API's version of the detail page's Replace checkbox: a body that
    does not mention a credential is a request this gateway never reads one out
    of, which is what makes "leaving it untouched preserves the stored one" true
    by construction rather than by care.

    ``spec_url`` is not editable. A server *is* its document; pointing an
    existing row at a different one would keep every stored operation, override
    and tool name while changing what they describe. Delete and create instead.
    """

    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    slug: str | None = None
    tool_prefix: str | None = None
    base_url: str | None = None
    enabled: bool | None = None
    auto_refresh: bool | None = None

    credential: Credential | None = None
    spec_auth_mode: SpecAuthMode | None = None
    spec_credential: Credential | None = None

    @field_validator("base_url")
    @classmethod
    def _usable_base_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        url = value.strip()
        if url and not url.lower().startswith(SCHEMES):
            raise ValueError(BASE_URL_SCHEME)
        return url

    @property
    def given(self) -> set[str]:
        """The fields this body actually carried, ``null`` ones included."""
        return set(self.model_fields_set)

    def as_patch(self) -> repo.ServerPatch:
        """The repository's patch, carrying exactly the keys that were sent."""
        values: dict[str, Any] = {
            name: getattr(self, name) for name in self.model_fields_set if name in _PATCHABLE
        }
        # The identifiers are derived from names the same way the detail page
        # derives them, so an API caller and an operator typing into the form
        # get the same slug out of the same words.
        if "slug" in values and values["slug"] is not None:
            values["slug"] = server_slug(str(values["slug"]))
        if "tool_prefix" in values and values["tool_prefix"] is not None:
            values["tool_prefix"] = sanitize(str(values["tool_prefix"]))
        return repo.ServerPatch(**values)


class OperationUpdate(BaseModel):
    """``PATCH /operations/{id}``: a tick, a name, a description.

    The two overrides follow the same rule as everything else here: absent
    leaves the stored value alone, and ``null`` clears it — and clearing a tool
    name override is how an operation goes back to its generated default
    (spec §5.3).
    """

    model_config = ConfigDict(extra="forbid")

    selected: bool | None = None
    tool_name_override: str | None = None
    description_override: str | None = None

    @property
    def given(self) -> set[str]:
        return set(self.model_fields_set)


__all__ = [
    "CUSTOM_NEEDS_CREDENTIAL",
    "MAX_UNKNOWN_SHOWN",
    "UNKNOWN_SELECTION",
    "Health",
    "OperationList",
    "OperationUpdate",
    "PreviewedOperation",
    "ServerCreate",
    "ServerList",
    "ServerUpdate",
    "SpecPreviewIn",
    "SpecPreviewOut",
    "WarningOut",
    "health_report",
    "previewed",
]
