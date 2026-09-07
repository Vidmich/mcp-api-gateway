"""The one shape every failure under ``/api/v1`` arrives in (spec §7.3).

The pages and the API fail differently on purpose. A browser that asks for a
server which is not there should get a page saying so, and a script should get
something it can branch on without parsing prose. So every refusal the JSON API
makes is this object and nothing else:

.. code-block:: json

   {"status": 404, "code": "not_found", "message": "No server with id 7.",
    "fields": {}}

Three parts, because each answers a different question. ``status`` is the HTTP
status repeated in the body, so a caller that has already read the response into
JSON does not have to reach back for it. ``code`` is the part a script matches
on — stable across releases, and never rewritten when the sentence is. And
``message`` is the sentence, which is for whoever ends up reading the log.

``fields`` is the fourth part, and it is empty for most failures. It carries the
per-field faults of a submission that could not be read, keyed by the field that
caused each one, so a caller that got two things wrong hears about both at once
rather than once per round trip. It is the same mapping the forms put beside
their inputs, which is what stops the API and the pages from disagreeing about
what is wrong with the same request.

**Nothing here knows about a route.** :class:`ApiFault` is an ordinary
:class:`~fastapi.HTTPException` carrying a code and, when it has them, fields;
anything that can raise an ``HTTPException`` — a missing database, a gateway
with no encryption key — therefore lands in the envelope without having heard
of it. That is why this module imports nothing from the rest of the web layer:
:mod:`~mcp_gateway.web.shell` renders the envelope for API paths,
:mod:`~mcp_gateway.web.auth` answers a failed guard with one, and neither has to
import the other.
"""

from __future__ import annotations

from typing import Any, Final

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import JSONResponse

#: A request arrived without a usable session (spec §3.3).
UNAUTHENTICATED: Final = "unauthenticated"
#: The thing named by the URL does not exist.
NOT_FOUND: Final = "not_found"
#: The body could not be read. ``fields`` says which parts of it.
INVALID_REQUEST: Final = "invalid_request"
#: A tool name this would publish is one another server already publishes
#: (spec §5.3). The request is fine; the world it would land in says no.
NAME_TAKEN: Final = "name_taken"
#: A spec URL could not be fetched, or what came back was not a spec.
SPEC_UNREADABLE: Final = "spec_unreadable"
#: Something was asked of the server the gateway provides itself that it does
#: not do — deleted, refreshed, or edited beyond its switch (task 102). A 409
#: rather than a 403: the caller is allowed to ask, and it is what the row *is*
#: that says no.
BUILTIN_SERVER: Final = "builtin_server"
#: The gateway is up but cannot serve this: no database, or no encryption key.
UNAVAILABLE: Final = "unavailable"
INTERNAL_ERROR: Final = "internal_error"

#: The code a plain :class:`~fastapi.HTTPException` gets, by its status. Kept
#: small on purpose: a status with no entry here is a failure nobody has
#: designed a code for yet, and :data:`FALLBACK_CODE` says exactly that rather
#: than inventing one that scripts would start matching on.
BY_STATUS: Final[dict[int, str]] = {
    400: "bad_request",
    401: UNAUTHENTICATED,
    403: "forbidden",
    404: NOT_FOUND,
    405: "method_not_allowed",
    409: "conflict",
    415: "unsupported_media_type",
    422: INVALID_REQUEST,
    500: INTERNAL_ERROR,
    503: UNAVAILABLE,
}

FALLBACK_CODE: Final = "error"

#: What a 500 says. Deliberately without detail: an unhandled exception's
#: message is written for a log, and the log is where it goes.
INTERNAL_MESSAGE: Final = "The gateway failed to handle this request."


class ApiError(BaseModel):
    """The body of every failed ``/api/v1`` request."""

    model_config = ConfigDict(frozen=True)

    status: int
    code: str
    message: str
    #: Per-field faults, keyed by field name. Empty unless the failure was one
    #: of a submission that could not be read.
    fields: dict[str, str] = Field(default_factory=dict)


def code_for(status: int) -> str:
    """The machine-readable code a bare status maps onto."""
    return BY_STATUS.get(status, FALLBACK_CODE)


class ApiFault(HTTPException):
    """A refusal with a code and, sometimes, the fields that caused it.

    An ``HTTPException`` so that raising one from a route needs no special
    handling anywhere: FastAPI already stops the request and the handler
    registered in :mod:`~mcp_gateway.web.shell` already renders it.
    """

    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        *,
        fields: dict[str, str] | None = None,
    ) -> None:
        self.code = code
        self.fields = dict(fields or {})
        super().__init__(status_code=status_code, detail=message)


def api_error(
    status: int, message: str, *, code: str | None = None, fields: dict[str, str] | None = None
) -> JSONResponse:
    """Render one failure as the envelope."""
    body = ApiError(
        status=status,
        code=code or code_for(status),
        message=message,
        fields=dict(fields or {}),
    )
    return JSONResponse(body.model_dump(), status_code=status)


def from_http_exception(exc: StarletteHTTPException) -> JSONResponse:
    """The envelope for anything raised as an ``HTTPException``.

    An :class:`ApiFault` brings its own code and fields; anything else — a 503
    from the session dependency, a 405 from the router — gets the code its
    status maps onto and whatever it put in ``detail``.
    """
    code = getattr(exc, "code", None)
    fields = getattr(exc, "fields", None)
    response = api_error(
        exc.status_code,
        _message(exc),
        code=code if isinstance(code, str) else None,
        fields=fields if isinstance(fields, dict) else None,
    )
    if exc.headers:
        response.headers.update(exc.headers)
    return response


def field_faults(errors: list[dict[str, Any]]) -> dict[str, str]:
    """Pydantic's per-error records, keyed by the field each one belongs to.

    ``loc`` arrives as ``("body", "spec_auth_mode")`` — the first element says
    where in the request it was found, which the caller already knows, so only
    the rest is a field name. Numbers among the rest are kept, because
    ``selected.0`` names the entry of a list that was wrong.

    A location made *only* of numbers is not a field at all: a body that is not
    JSON reports the character the decoder gave up at, and ``{"1": "JSON decode
    error"}`` is a key a caller might reasonably start matching on. Those keep
    the location word instead. A fault with no location at all keeps a name too,
    because a 422 that lists nothing is a 422 nobody can act on.
    """
    faults: dict[str, str] = {}
    for error in errors:
        location = tuple(error.get("loc", ()))
        parts = location[1:] if location[:1] in (("body",), ("query",), ("path",)) else location
        if any(isinstance(part, str) for part in parts):
            name = ".".join(str(part) for part in parts)
        else:
            name = str(location[0]) if location else "request"
        faults.setdefault(name, str(error.get("msg", "")).removeprefix("Value error, "))
    return faults


def _message(exc: StarletteHTTPException) -> str:
    """A sentence for a failure, whatever ``detail`` turned out to hold.

    Starlette fills ``detail`` in from the status when a route raises without
    one, so this is only reached by a caller that passed something falsy on
    purpose; the code, spelled out, is a better answer than an empty string.
    """
    detail = exc.detail
    if detail:
        return str(detail)
    return BY_STATUS.get(exc.status_code, FALLBACK_CODE).replace("_", " ").capitalize()


__all__ = [
    "BUILTIN_SERVER",
    "BY_STATUS",
    "FALLBACK_CODE",
    "INTERNAL_ERROR",
    "INTERNAL_MESSAGE",
    "INVALID_REQUEST",
    "NAME_TAKEN",
    "NOT_FOUND",
    "SPEC_UNREADABLE",
    "UNAUTHENTICATED",
    "UNAVAILABLE",
    "ApiError",
    "ApiFault",
    "api_error",
    "code_for",
    "field_faults",
    "from_http_exception",
]
