"""The optional bearer token on ``/mcp`` (spec §3.2, §6, task 126)."""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.types import Receive, Scope, Send

from mcp_gateway.app import HEALTH_PATH, create_app
from mcp_gateway.config import Settings, load_settings
from mcp_gateway.db import repo
from mcp_gateway.db.models import Base
from mcp_gateway.db.session import Database, open_database
from mcp_gateway.mcpsrv.auth import (
    CHALLENGE,
    DIGEST_KEY,
    ENABLED_KEY,
    FALSE,
    FROM_DATABASE,
    SET_AT_KEY,
    TRUE,
    UNAUTHORIZED,
    BearerGuard,
    McpAuth,
    app_auth,
    configured,
    digest_of,
    forget,
    load_auth,
    mcp_auth_service,
    presented_token,
    protect,
    resolve,
    store_open,
    store_token,
    stored_token,
    warn_if_open,
)
from mcp_gateway.mcpsrv.server import MCPEndpoint, mcp_service

TOKEN = "s3cret-token"

#: What the Configuration page would have stored: long enough to be taken there,
#: which the file's shorter one above deliberately is not.
LONG_TOKEN = "L" * 40


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
async def database(tmp_path: Path) -> AsyncIterator[Database]:
    db = open_database(settings_for(tmp_path))
    async with db.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    try:
        yield db
    finally:
        await db.dispose()


@pytest.fixture
async def session(database: Database) -> AsyncIterator[AsyncSession]:
    async with database.session_factory() as opened:
        yield opened


#: What the streamable HTTP transport requires of a POST.
MCP_HEADERS = {
    "content-type": "application/json",
    "accept": "application/json, text/event-stream",
}

#: The one request a client may send before it has a session, which is what a
#: test of the *guard* needs: anything else would be refused by the transport
#: for a reason that has nothing to do with the token.
HANDSHAKE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "test-client", "version": "1.0"},
    },
}


def settings_for(tmp_path: Path, body: str = "") -> Settings:
    config = tmp_path / "config.toml"
    config.write_text(body, encoding="utf-8")
    return load_settings({"config": str(config)}, environ={})


def guarded(tmp_path: Path, token: str = TOKEN) -> Settings:
    return settings_for(tmp_path, f'[mcp]\nauth_token = "{token}"\n')


def guard_for(token: str | None) -> BearerGuard:
    """A guard over a spy, requiring ``token`` — or requiring nothing."""
    auth = McpAuth() if token is None else McpAuth(digest=digest_of(token))
    return BearerGuard(Spy(), lambda: auth)


def scope_with(*headers: tuple[str, str]) -> Scope:
    """An HTTP scope carrying the given headers, as ASGI delivers them.

    Encoded as UTF-8, which is what a client sending a non-ASCII token puts on
    the wire — latin-1 could not carry one at all.
    """
    return {
        "type": "http",
        "headers": [(name.encode("utf-8"), value.encode("utf-8")) for name, value in headers],
    }


class Spy:
    """An ASGI application that records whether it was reached."""

    def __init__(self) -> None:
        self.calls = 0

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        self.calls += 1
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def post(client: TestClient, headers: dict[str, str] | None = None) -> Any:
    return client.post("/mcp", headers={**MCP_HEADERS, **(headers or {})}, json=HANDSHAKE)


# --- reading the header ------------------------------------------------------


def test_a_bearer_header_yields_its_token() -> None:
    assert presented_token(scope_with(("authorization", "Bearer abc"))) == b"abc"


def test_the_scheme_is_case_insensitive() -> None:
    # RFC 7235 says so, and a client that sends ``bearer`` is not wrong.
    assert presented_token(scope_with(("Authorization", "bearer abc"))) == b"abc"


def test_extra_whitespace_around_the_token_is_forgiven() -> None:
    assert presented_token(scope_with(("authorization", "Bearer   abc  "))) == b"abc"


def test_another_scheme_offers_no_token() -> None:
    assert presented_token(scope_with(("authorization", "Basic dXNlcjpwdw=="))) is None


def test_a_scheme_with_nothing_after_it_offers_no_token() -> None:
    assert presented_token(scope_with(("authorization", "Bearer"))) is None


def test_no_header_at_all_offers_no_token() -> None:
    assert presented_token(scope_with()) is None


def test_the_first_authorization_header_is_the_one_that_counts() -> None:
    scope = scope_with(("authorization", "Bearer first"), ("authorization", "Bearer second"))

    assert presented_token(scope) == b"first"


# --- the guard itself --------------------------------------------------------


def test_the_right_token_reaches_the_application() -> None:
    assert guard_for(TOKEN).authorized(scope_with(("authorization", f"Bearer {TOKEN}"))) is True


def test_a_wrong_token_does_not() -> None:
    assert guard_for(TOKEN).authorized(scope_with(("authorization", "Bearer nope"))) is False


def test_a_prefix_of_the_token_is_not_enough() -> None:
    # The comparison is over digests, so it cannot short-circuit on length.
    guard = guard_for(TOKEN)

    assert guard.authorized(scope_with(("authorization", f"Bearer {TOKEN[:-1]}"))) is False


def test_a_token_that_is_not_ascii_still_works() -> None:
    # A config file carries any string, so the check has to survive one. The
    # client sends UTF-8; decoding that as latin-1 on the way in would compare
    # mojibake against the real token and never match.
    secret = "pässwörd-ключ"

    assert guard_for(secret).authorized(scope_with(("authorization", f"Bearer {secret}"))) is True


def test_an_open_endpoint_admits_everyone() -> None:
    # Including a caller that offered a token anyway: there is nothing here for
    # it to be wrong against.
    guard = guard_for(None)

    assert guard.authorized(scope_with()) is True
    assert guard.authorized(scope_with(("authorization", "Bearer anything"))) is True


def test_the_guard_is_there_whatever_the_configuration() -> None:
    """Which is the change task 126 made, and the reason for it.

    An open endpoint used to be the bare application. It cannot be any more: the
    token moves while the process runs, and what sits on the router does not.
    """
    inner = Spy()

    assert protect(inner, lambda: McpAuth()) is not inner


def test_the_guard_asks_again_on_every_request() -> None:
    """A digest captured when the route was built would be the process's forever."""
    answers = [McpAuth(), McpAuth(digest=digest_of(TOKEN))]
    guard = BearerGuard(Spy(), lambda: answers[-1] if len(answers) == 1 else answers.pop(0))

    assert guard.authorized(scope_with()) is True
    assert guard.authorized(scope_with()) is False


def test_a_gateway_with_nothing_on_its_state_is_open() -> None:
    # An app assembled by hand in a test is still an app whose endpoint answers.
    app = FastAPI()

    assert app_auth(app)().required is False


# --- over the wire -----------------------------------------------------------


def test_the_configured_token_passes(tmp_path: Path) -> None:
    app = create_app(guarded(tmp_path), services=[mcp_service])

    with TestClient(app) as client:
        assert post(client, bearer(TOKEN)).status_code == 200


def test_a_wrong_token_is_refused_with_a_challenge(tmp_path: Path) -> None:
    app = create_app(guarded(tmp_path), services=[mcp_service])

    with TestClient(app) as client:
        response = post(client, bearer("wrong"))

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == CHALLENGE
    assert response.json()["error"] == UNAUTHORIZED


def test_a_missing_token_is_refused_the_same_way(tmp_path: Path) -> None:
    # Same status, same challenge, same body: which of the two it was is not
    # something the caller gets to learn.
    app = create_app(guarded(tmp_path), services=[mcp_service])

    with TestClient(app) as client:
        response = post(client)

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == CHALLENGE
    assert response.json()["error"] == UNAUTHORIZED


def test_without_a_token_configured_the_endpoint_is_open(tmp_path: Path) -> None:
    app = create_app(settings_for(tmp_path), services=[mcp_service])

    with TestClient(app) as client:
        assert post(client).status_code == 200


def test_an_admin_cookie_alone_does_not_open_the_endpoint(tmp_path: Path) -> None:
    # The two auth systems are independent (spec §3.3). Task 018 has not built
    # the session cookie yet; this pins the boundary before it can be crossed.
    app = create_app(guarded(tmp_path), services=[mcp_service])

    with TestClient(app) as client:
        client.cookies.set("session", "a-perfectly-valid-looking-admin-session")
        response = post(client)

    assert response.status_code == 401


def test_the_token_guards_only_the_mcp_route(tmp_path: Path) -> None:
    # ``/healthz`` is never behind auth of either kind (spec §3.3).
    app = create_app(guarded(tmp_path), services=[mcp_service])

    with TestClient(app) as client:
        assert client.get(HEALTH_PATH).status_code == 200


def test_the_guard_moves_with_the_configured_path(tmp_path: Path) -> None:
    settings = settings_for(tmp_path, f'[mcp]\npath = "/gw"\nauth_token = "{TOKEN}"\n')
    app = create_app(settings, services=[mcp_service])

    with TestClient(app) as client:
        assert client.post("/gw", headers=MCP_HEADERS, json=HANDSHAKE).status_code == 401


def test_an_unauthenticated_request_never_reaches_the_session_manager(tmp_path: Path) -> None:
    # An app with no services answers 503 from inside the endpoint. Getting 401
    # instead is the proof that the check ran before the endpoint did — so no
    # session was opened, and no stream allocated, for a caller with no token.
    app = create_app(guarded(tmp_path))
    endpoint: MCPEndpoint = app.state.mcp

    with TestClient(app) as client:
        response = post(client)

    assert endpoint.running is False
    assert response.status_code == 401
    assert "mcp-session-id" not in response.headers


# --- what is in force ---------------------------------------------------------


def test_a_file_with_no_token_leaves_the_endpoint_open(tmp_path: Path) -> None:
    assert configured(settings_for(tmp_path).mcp).required is False


def test_a_file_with_a_token_requires_it(tmp_path: Path) -> None:
    auth = configured(guarded(tmp_path).mcp)

    assert auth.required is True
    assert auth.stored is False
    assert auth.accepts(TOKEN.encode()) is True


@pytest.mark.anyio
async def test_a_silent_table_leaves_the_file_deciding(
    session: AsyncSession, tmp_path: Path
) -> None:
    assert await stored_token(session) is None

    auth = await load_auth(session, guarded(tmp_path))

    assert auth.stored is False
    assert auth.accepts(TOKEN.encode()) is True


@pytest.mark.anyio
async def test_a_stored_token_overrides_the_file_entirely(
    session: AsyncSession, tmp_path: Path
) -> None:
    """The rule ``[admin]`` set: the table wins whole, or not at all."""
    await store_token(session, LONG_TOKEN)

    auth = await load_auth(session, guarded(tmp_path))

    assert auth.stored is True
    assert auth.accepts(LONG_TOKEN.encode()) is True
    assert auth.accepts(TOKEN.encode()) is False


@pytest.mark.anyio
async def test_switching_it_off_opens_the_endpoint_whatever_the_file_says(
    session: AsyncSession, tmp_path: Path
) -> None:
    await store_token(session, LONG_TOKEN)
    await store_open(session)

    auth = await load_auth(session, guarded(tmp_path))

    assert auth.required is False
    # The table is why, which is not the same as nothing being configured
    # anywhere — and is what lets the page tell the two apart.
    assert auth.stored is True


@pytest.mark.anyio
async def test_switching_it_off_keeps_the_token(session: AsyncSession) -> None:
    """So switching back on does not mean issuing a new one to every client."""
    await store_token(session, LONG_TOKEN)
    await store_open(session)

    stored = await stored_token(session)

    assert stored is not None
    assert stored.enabled is False
    assert stored.digest == digest_of(LONG_TOKEN)


@pytest.mark.anyio
async def test_switching_it_back_on_without_a_token_uses_the_stored_one(
    session: AsyncSession, tmp_path: Path
) -> None:
    await store_token(session, LONG_TOKEN, now=dt.datetime(2026, 1, 1, tzinfo=dt.UTC))
    await store_open(session)
    await store_token(session)

    auth = await load_auth(session, settings_for(tmp_path))

    assert auth.accepts(LONG_TOKEN.encode()) is True
    # And the moment it was set is the moment it was set, not the moment it was
    # switched back on.
    assert auth.set_at == dt.datetime(2026, 1, 1, tzinfo=dt.UTC)


@pytest.mark.anyio
async def test_a_replaced_token_refuses_the_old_one(session: AsyncSession, tmp_path: Path) -> None:
    await store_token(session, LONG_TOKEN)
    await store_token(session, "N" * 40)

    auth = await load_auth(session, settings_for(tmp_path))

    assert auth.accepts(b"N" * 40) is True
    assert auth.accepts(LONG_TOKEN.encode()) is False


@pytest.mark.anyio
async def test_forgetting_the_rows_hands_the_decision_back(
    session: AsyncSession, tmp_path: Path
) -> None:
    await store_token(session, LONG_TOKEN)

    assert await forget(session) is True
    assert await forget(session) is False

    auth = await load_auth(session, guarded(tmp_path))

    assert auth.stored is False
    assert auth.accepts(TOKEN.encode()) is True


# --- rows nobody should have written ------------------------------------------


@pytest.mark.anyio
async def test_an_enabled_row_with_no_digest_falls_back_to_the_file(
    session: AsyncSession, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """It can only have got there by hand, and refusing every call is a worse
    answer than saying so and using what the file says."""
    await repo.set_setting(session, ENABLED_KEY, TRUE)

    with caplog.at_level(logging.ERROR, logger="mcp_gateway.mcpsrv.auth"):
        auth = await load_auth(session, guarded(tmp_path))

    assert auth.stored is False
    assert auth.accepts(TOKEN.encode()) is True
    assert DIGEST_KEY in caplog.text


@pytest.mark.anyio
async def test_a_digest_that_is_not_one_is_treated_the_same_way(
    session: AsyncSession, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    await repo.set_setting(session, ENABLED_KEY, TRUE)
    await repo.set_setting(session, DIGEST_KEY, "not-a-digest")

    with caplog.at_level(logging.ERROR, logger="mcp_gateway.mcpsrv.auth"):
        auth = await load_auth(session, guarded(tmp_path))

    assert auth.stored is False
    assert caplog.records != []


@pytest.mark.anyio
async def test_sixty_four_characters_that_are_not_hex_are_not_a_digest(
    session: AsyncSession, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    await repo.set_setting(session, ENABLED_KEY, TRUE)
    await repo.set_setting(session, DIGEST_KEY, "z" * 64)

    with caplog.at_level(logging.ERROR, logger="mcp_gateway.mcpsrv.auth"):
        assert (await load_auth(session, guarded(tmp_path))).stored is False


@pytest.mark.anyio
async def test_an_unreadable_timestamp_costs_a_sentence_and_not_the_token(
    session: AsyncSession, tmp_path: Path
) -> None:
    await store_token(session, LONG_TOKEN)
    await repo.set_setting(session, SET_AT_KEY, "last Tuesday")

    auth = await load_auth(session, settings_for(tmp_path))

    assert auth.accepts(LONG_TOKEN.encode()) is True
    assert auth.set_at is None


@pytest.mark.anyio
async def test_anything_but_true_means_off(session: AsyncSession, tmp_path: Path) -> None:
    await repo.set_setting(session, ENABLED_KEY, FALSE)

    assert (await load_auth(session, guarded(tmp_path))).required is False


# --- the warning --------------------------------------------------------------


def test_an_open_endpoint_is_announced(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    settings = settings_for(tmp_path)

    with caplog.at_level(logging.WARNING, logger="mcp_gateway.mcpsrv.auth"):
        said = warn_if_open(settings, McpAuth())

    assert said is not None
    assert settings.mcp.path in said
    assert "Configuration page" in said
    assert said in caplog.text


def test_a_guarded_endpoint_is_not(tmp_path: Path) -> None:
    guarded_auth = McpAuth(digest=digest_of(TOKEN))

    assert warn_if_open(settings_for(tmp_path), guarded_auth) is None


def test_a_token_stored_on_the_page_silences_the_warning(tmp_path: Path) -> None:
    # The file has none, so reading ``settings`` here would warn about a door
    # that is shut (task 126).
    stored = McpAuth(digest=digest_of(LONG_TOKEN), source=FROM_DATABASE)

    assert warn_if_open(settings_for(tmp_path), stored) is None


def test_opening_it_from_the_page_brings_the_warning_back(tmp_path: Path) -> None:
    assert warn_if_open(guarded(tmp_path), McpAuth(source=FROM_DATABASE)) is not None


# --- over the wire, without a restart -----------------------------------------


@pytest.mark.anyio
async def test_the_service_reads_the_stored_token_over_the_file(
    database: Database, tmp_path: Path
) -> None:
    app = create_app(guarded(tmp_path), services=())
    app.state.db = database
    async with database.session() as opened:
        await store_token(opened, LONG_TOKEN)

    async with mcp_auth_service(app):
        auth: McpAuth = app.state.mcp_auth

    assert auth.stored is True
    assert auth.accepts(LONG_TOKEN.encode()) is True


def test_a_token_put_on_the_state_is_required_from_the_next_request(tmp_path: Path) -> None:
    """No restart, and no second route: the guard asks ``app.state`` per request."""
    app = create_app(settings_for(tmp_path), services=[mcp_service])

    with TestClient(app) as client:
        assert post(client).status_code == 200

        app.state.mcp_auth = McpAuth(digest=digest_of(LONG_TOKEN), source=FROM_DATABASE)

        assert post(client).status_code == 401
        assert post(client, bearer(LONG_TOKEN)).status_code == 200


def test_taking_it_off_the_state_opens_the_endpoint_again(tmp_path: Path) -> None:
    app = create_app(guarded(tmp_path), services=[mcp_service])

    with TestClient(app) as client:
        assert post(client).status_code == 401

        app.state.mcp_auth = McpAuth(source=FROM_DATABASE)

        assert post(client).status_code == 200


def test_the_stored_token_is_what_the_endpoint_checks(tmp_path: Path) -> None:
    """And the file's is then not a way in, which is what "wins whole" means."""
    app = create_app(guarded(tmp_path), services=[mcp_service])
    app.state.mcp_auth = McpAuth(digest=digest_of(LONG_TOKEN), source=FROM_DATABASE)

    with TestClient(app) as client:
        assert post(client, bearer(TOKEN)).status_code == 401
        assert post(client, bearer(LONG_TOKEN)).status_code == 200


def test_resolving_never_carries_the_token_itself(tmp_path: Path) -> None:
    """Only ever a digest, on the object every page and every log line reads."""
    auth = resolve(guarded(tmp_path), None)

    assert TOKEN not in repr(auth)
    assert TOKEN.encode() not in (auth.digest or b"")
