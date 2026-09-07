"""The JSON API under ``/api/v1`` (spec §7.3).

Every configuration action the pages offer, addressable by a script. Same
session, same permissions, same rules — and, wherever something is written, the
same function: what the models mean is :mod:`mcp_gateway.web.api`, what a create
does is :func:`mcp_gateway.web.picker.register`, what an edit does is
:mod:`mcp_gateway.web.detail`. The routes here read a request, hand it over, and
choose a status code.

**A refusal is translated once.** :func:`answered` is the one place where what
the rules raise becomes what a caller reads: a missing row is a 404, a request
that could not be read is a 422 naming the fields, a tool name somebody else
publishes is a 409. Doing it in a context manager rather than in each route is
what stops the same failure from arriving as two different codes depending on
which endpoint met it.

**A script is never redirected.** :func:`mcp_gateway.web.auth.unauthenticated`
answers an unauthenticated request under this prefix with a 401 carrying the
envelope, not with a redirect to a login form — a caller following one would
read an HTML page as its result and see a success where there was none.

**Reads have no adapter.** The repository's own DTOs go out as they are; they
cannot carry a credential, so neither can a response. See
:mod:`mcp_gateway.web.api`.
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Iterator
from typing import Annotated, Final

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from mcp_gateway.config import Settings
from mcp_gateway.crypto import CredentialCipher
from mcp_gateway.db import repo
from mcp_gateway.db.models import Operation, OperationStatus, Server
from mcp_gateway.db.session import request_session
from mcp_gateway.mcpsrv.server import app_announcer
from mcp_gateway.naming import NamesTaken
from mcp_gateway.openapi.diagnostics import SpecError
from mcp_gateway.openapi.ingest import preview_spec
from mcp_gateway.refresh import RefreshLocks, refresh_server
from mcp_gateway.usage import (
    DEFAULT_GROUP_BY,
    DEFAULT_RANGE,
    GroupBy,
    UsageRange,
    UsageReport,
    usage_report,
)
from mcp_gateway.web.api import (
    CUSTOM_NEEDS_CREDENTIAL,
    Health,
    OperationList,
    OperationUpdate,
    RefreshOut,
    ServerCreate,
    ServerList,
    ServerUpdate,
    SpecPreviewIn,
    SpecPreviewOut,
    health_report,
    previewed,
    refreshed,
)
from mcp_gateway.web.auth import API_PREFIX, require_session
from mcp_gateway.web.detail import SettingsInvalid, apply_operation, apply_patch
from mcp_gateway.web.errors import (
    INVALID_REQUEST,
    NAME_TAKEN,
    NOT_FOUND,
    SPEC_UNREADABLE,
    ApiFault,
    api_error,
    field_faults,
)
from mcp_gateway.web.picker import NO_BASE_URL, register
from mcp_gateway.web.routes_ui import NO_CIPHER
from mcp_gateway.web.shell import under
from mcp_gateway.web.wizard import (
    NOTHING_TO_REUSE,
    PendingServer,
    failure_field,
    failure_message,
)

logger = logging.getLogger(__name__)

SERVERS_PATH: Final = f"{API_PREFIX}/servers"
SERVER_PATH: Final = f"{SERVERS_PATH}/{{server_id}}"
ACKNOWLEDGE_PATH: Final = f"{SERVER_PATH}/acknowledge"
REFRESH_PATH: Final = f"{SERVER_PATH}/refresh"
SERVER_OPERATIONS_PATH: Final = f"{SERVER_PATH}/operations"
#: Addressed by row id and not under its server, exactly as spec §7.3 writes it:
#: an operation id is unique on its own, and a caller holding one from a list
#: should not have to remember which server it came from to edit it.
OPERATION_PATH: Final = f"{API_PREFIX}/operations/{{operation_id}}"
PREVIEW_PATH: Final = f"{API_PREFIX}/specs/preview"
METRICS_PATH: Final = f"{API_PREFIX}/metrics"
HEALTH_PATH: Final = f"{API_PREFIX}/health"


@contextlib.contextmanager
def answered() -> Iterator[None]:
    """Turn what the rules raise into the envelope a caller reads.

    The four things any write here can raise, and the status each one means:

    * a row that is not there — 404, because the URL named nothing;
    * a request that could not be read — 422, with the offending fields, since
      the same body sent again would fail the same way;
    * a tool name another server publishes — 409, because the request is fine
      and it is the world it would land in that says no (spec §5.3);
    * a document that could not be fetched or parsed — 422, beside the field
      that can fix it, which for a ``401`` from the spec URL is the spec-auth
      mode rather than the URL (spec §5.1).
    """
    try:
        yield
    except repo.ServerNotFound as missing:
        raise ApiFault(404, NOT_FOUND, str(missing)) from None
    except repo.OperationNotFound as missing:
        raise ApiFault(404, NOT_FOUND, str(missing)) from None
    except SettingsInvalid as invalid:
        raise ApiFault(
            422, INVALID_REQUEST, "; ".join(invalid.errors.values()), fields=dict(invalid.errors)
        ) from None
    except NamesTaken as taken:
        raise ApiFault(409, NAME_TAKEN, str(taken)) from None
    except SpecError as failure:
        raise ApiFault(
            422,
            SPEC_UNREADABLE,
            failure_message(failure),
            fields={failure_field(failure): failure_message(failure)},
        ) from None


#: One session per request, committed on the way out (see :mod:`~mcp_gateway.db.session`).
Session = Annotated[AsyncSession, Depends(request_session)]


def _cipher(request: Request) -> CredentialCipher:
    """The credential cipher, or a 503 saying why there is none (spec §3.2)."""
    cipher: CredentialCipher | None = request.app.state.cipher
    if cipher is None:
        raise HTTPException(status_code=503, detail=NO_CIPHER)
    return cipher


def _locks(request: Request) -> RefreshLocks:
    """The registry that keeps two refreshes of one server apart (spec §8)."""
    locks: RefreshLocks = request.app.state.refresh_locks
    return locks


async def _operation(session: AsyncSession, operation_id: int) -> Operation:
    """One operation by row id, or the 404 that says there is none."""
    operation = await repo.get_operation(session, operation_id)
    if operation is None:
        raise repo.OperationNotFound(operation_id)
    return operation


def api_router() -> APIRouter:
    """Every ``/api/v1`` route, all of them behind a session."""
    router = APIRouter(
        tags=["api"],
        # Declared on the router rather than per route: an endpoint added later
        # is protected by being on it, instead of by somebody remembering.
        dependencies=[Depends(require_session)],
    )

    @router.get(HEALTH_PATH, summary="How the gateway is")
    async def health(request: Request) -> Health:
        """The same body ``/healthz`` serves, from the same function.

        Behind the session here and open there, which is the only difference
        between them: a probe needs the open one, and a caller already signed in
        should not have to leave the API to ask.
        """
        return health_report(request.app.state.settings, request.app.state.started_at)

    @router.get(SERVERS_PATH, summary="Every registered server")
    async def list_servers(session: Session) -> ServerList:
        return ServerList(servers=tuple(await repo.list_servers(session)))

    @router.post(SERVERS_PATH, status_code=201, summary="Register a server from its spec")
    async def create_server(
        request: Request, body: ServerCreate, session: Session, response: Response
    ) -> repo.ServerDetail:
        """Fetch the document, then register what it describes — or none of it.

        The wizard's two steps in one call, running through the wizard's own
        save: the fetch is :func:`~mcp_gateway.openapi.ingest.preview_spec` and
        the write is :func:`~mcp_gateway.web.picker.register`, which is what
        makes the row this leaves behind the row step 2 would have left.
        """
        cipher = _cipher(request)
        settings: Settings = request.app.state.settings
        form = body.as_form(name=body.name.strip())
        with answered():
            preview = await preview_spec(
                form.spec_url,
                spec_credential=form.fetch_credential,
                api_credential=form.credential,
                http=settings.http,
                # Whatever pool the process shares (spec §2); ``None`` in an app
                # built without services, where a client is made for the call.
                client=request.app.state.http_client,
            )
        pending = PendingServer(form=form, preview=preview)
        if not pending.base_url:
            # A document that never said where its API lives, and a caller who
            # did not say either. Refused here rather than stored as a server
            # whose tools would have nowhere to call.
            raise ApiFault(422, INVALID_REQUEST, NO_BASE_URL, fields={"base_url": NO_BASE_URL})
        try:
            selection = body.selection(pending)
        except ValueError as unknown:
            raise ApiFault(
                422, INVALID_REQUEST, str(unknown), fields={"selected": str(unknown)}
            ) from None

        with answered():
            server = await register(
                session,
                pending,
                prefix=body.prefix_for(pending),
                selection=selection,
                cipher=cipher,
            )
        response.headers["Location"] = f"{SERVERS_PATH}/{server.id}"
        return await repo.server_detail(session, server.id)

    @router.get(SERVER_PATH, summary="One server and its operations")
    async def get_server(server_id: int, session: Session) -> repo.ServerDetail:
        with answered():
            return await repo.server_detail(session, server_id)

    @router.patch(SERVER_PATH, summary="Change a server's settings")
    async def update_server(
        request: Request, server_id: int, body: ServerUpdate, session: Session
    ) -> repo.ServerDetail:
        """Apply the fields the body carried, all of them or none.

        The two refusals happen before anything is written — an identifier
        another server holds, and a prefix whose names another server already
        publishes — so a caller that gets one back is describing the server as
        it still is.
        """
        cipher = _cipher(request)
        with answered():
            server = await repo.require_server(session, server_id)
            _spec_auth_is_coherent(body, server)
            await apply_patch(session, server, body.as_patch(), cipher=cipher)
            return await repo.server_detail(session, server_id)

    @router.delete(SERVER_PATH, status_code=204, summary="Delete a server")
    async def delete_server(server_id: int, session: Session) -> Response:
        with answered():
            doomed = await repo.require_server(session, server_id)
            name = doomed.name
            await repo.delete_server(session, server_id)
        logger.info("Deleted server %r through the API", name)
        return Response(status_code=204)

    @router.post(ACKNOWLEDGE_PATH, summary="Clear Needs Attention")
    async def acknowledge(server_id: int, session: Session) -> repo.ServerDetail:
        """Mark this server's unreviewed operations as seen (spec §5.4).

        The whole server comes back rather than a bare acknowledgement, because
        what acknowledging *did* is spread across every operation it settled.
        """
        with answered():
            await repo.acknowledge_server(session, server_id)
            return await repo.server_detail(session, server_id)

    @router.post(REFRESH_PATH, summary="Re-read this server's spec")
    async def refresh(request: Request, server_id: int, session: Session) -> RefreshOut:
        """Fetch the document again and reconcile it (spec §5.4).

        Answers 200 whatever the refresh turned out to be, including a document
        that could not be read: the gateway went and looked, and what it found
        is now recorded against the row, which is a thing that happened rather
        than a request that was wrong. ``outcome`` is what a caller branches on.

        The one refusal is a server id nothing answers to, which
        :func:`answered` turns into the 404 it is.
        """
        cipher = _cipher(request)
        settings: Settings = request.app.state.settings
        with answered():
            # Queues behind a refresh of this server that is already running,
            # the scheduler's included, rather than running a second one beside
            # it (spec §8).
            async with _locks(request).hold(server_id):
                report = await refresh_server(
                    session,
                    server_id,
                    cipher=cipher,
                    http=settings.http,
                    client=request.app.state.http_client,
                    announce=app_announcer(request.app),
                )
        return refreshed(report)

    @router.get(SERVER_OPERATIONS_PATH, summary="One server's operations")
    async def list_operations(
        server_id: int,
        session: Session,
        status: Annotated[OperationStatus | None, Query()] = None,
    ) -> OperationList:
        with answered():
            # Asked for its own sake, so that a server with no operations and a
            # server that does not exist are different answers.
            await repo.require_server(session, server_id)
            rows = await repo.list_operations(session, server_id, status=status)
        return OperationList(operations=tuple(rows))

    @router.patch(OPERATION_PATH, summary="Change one operation")
    async def update_operation(
        operation_id: int, body: OperationUpdate, session: Session
    ) -> repo.OperationView:
        """Apply a tick, a tool name, a description — or whichever were sent.

        A field the body left out is filled in from the row, because the write
        underneath takes all three: a name is only legal with respect to the
        operation it sits on, and checking one against a half-applied row would
        be checking it against something that never existed.
        """
        with answered():
            operation = await _operation(session, operation_id)
            given = body.given
            await apply_operation(
                session,
                operation,
                selected=(
                    body.selected
                    if "selected" in given and body.selected is not None
                    else operation.selected
                ),
                tool_name=(
                    (body.tool_name_override or "")
                    if "tool_name_override" in given
                    else (operation.tool_name_override or "")
                ),
                description=(
                    body.description_override
                    if "description_override" in given
                    else operation.description_override
                ),
            )
            return repo.to_view(operation)

    @router.get(METRICS_PATH, summary="Usage over time")
    async def metrics(
        request: Request,
        session: Session,
        range_: Annotated[UsageRange, Query(alias="range")] = DEFAULT_RANGE,
        group_by: Annotated[GroupBy, Query()] = DEFAULT_GROUP_BY,
    ) -> UsageReport:
        """The time series the monitoring page draws (spec §7.2).

        Both parameters are enumerations rather than free text, so a range this
        gateway does not draw is refused by the same 422 as a malformed body
        instead of quietly becoming a default — a caller asking for ``90d`` and
        being handed a day would have no way to notice.

        The window is worked out before anything is read, and the re-bucketing
        happens in SQL, so what crosses this boundary is the number of points
        that will be drawn rather than the number of buckets that were stored.

        The reading itself is :func:`~mcp_gateway.usage.usage_report`, which is
        also what the monitoring page calls: one definition of what a range
        means, so the chart and the endpoint cannot report different totals for
        the same hour.
        """
        settings: Settings = request.app.state.settings
        return await usage_report(
            session,
            range_,
            bucket_seconds=settings.metrics.bucket_seconds,
            group_by=group_by,
        )

    @router.post(PREVIEW_PATH, summary="Read a spec URL without saving anything")
    async def preview(request: Request, body: SpecPreviewIn) -> SpecPreviewOut:
        """Fetch and parse a document, storing nothing (spec §5.1).

        Takes its credentials inline, because the whole point is the unsaved
        case: there is no server yet to have stored any. They are used for this
        one request and kept nowhere — not in the database, and not in the
        preview store the wizard uses, which exists only because a browser has
        to come back for step 2.

        This route takes no database session. That is the plainest way to say a
        preview writes nothing: there is nothing for it to write with.
        """
        settings: Settings = request.app.state.settings
        form = body.as_form()
        with answered():
            return previewed(
                await preview_spec(
                    form.spec_url,
                    spec_credential=form.fetch_credential,
                    api_credential=form.credential,
                    http=settings.http,
                    client=request.app.state.http_client,
                )
            )

    return router


def _spec_auth_is_coherent(body: ServerUpdate, server: Server) -> None:
    """Refuse a spec-auth mode this server could not actually fetch with.

    The two ways a patch can leave the columns saying different things: reusing
    an API credential when the patch has just cleared it, or naming ``custom``
    with no credential supplied and none stored. Both would otherwise surface
    later as a 401 nobody can explain — or, for the second, as a ``ValueError``
    from inside the repository, which is true and no use to anybody.
    """
    given = body.given
    mode = body.spec_auth_mode if "spec_auth_mode" in given else server.spec_auth_mode
    if mode == "same_as_api":
        has_api = (
            body.credential is not None if "credential" in given else server.auth_type != "none"
        )
        if not has_api:
            raise SettingsInvalid({"spec_auth_mode": NOTHING_TO_REUSE})
    elif mode == "custom":
        supplied = body.spec_credential is not None if "spec_credential" in given else False
        stored = server.spec_auth_mode == "custom" and bool(server.spec_auth_config_encrypted)
        cleared = "spec_credential" in given and body.spec_credential is None
        if not supplied and (cleared or not stored):
            raise SettingsInvalid({"spec_credential": CUSTOM_NEEDS_CREDENTIAL})


def mount_api(app: FastAPI) -> None:
    """Add the JSON API to ``app``, and the one handler its shape needs.

    FastAPI's own validation failure is a list of records addressed by location;
    every other failure under this prefix already arrives as an
    :class:`~mcp_gateway.web.errors.ApiFault`. This turns the first into the
    same envelope as the rest, and leaves anything outside the prefix to
    FastAPI, which is what the pages already expect.
    """

    async def on_invalid_request(request: Request, exc: Exception) -> Response:
        if not isinstance(exc, RequestValidationError):  # pragma: no cover - fastapi's contract
            raise exc
        if not under(request.url.path, API_PREFIX):
            return await request_validation_exception_handler(request, exc)
        faults = field_faults(list(exc.errors()))
        return api_error(
            422,
            "; ".join(faults.values()) or "The request could not be read.",
            code=INVALID_REQUEST,
            fields=faults,
        )

    app.add_exception_handler(RequestValidationError, on_invalid_request)
    app.include_router(api_router())


__all__ = [
    "ACKNOWLEDGE_PATH",
    "HEALTH_PATH",
    "METRICS_PATH",
    "OPERATION_PATH",
    "PREVIEW_PATH",
    "REFRESH_PATH",
    "SERVERS_PATH",
    "SERVER_OPERATIONS_PATH",
    "SERVER_PATH",
    "answered",
    "api_router",
    "mount_api",
]
