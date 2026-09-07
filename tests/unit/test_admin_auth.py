"""Admin login: the hash, the cookie, the guard, and the mode with none of them.

Spec §3.3, task 018.
"""

from __future__ import annotations

import time
from http.cookies import SimpleCookie
from pathlib import Path
from typing import Annotated, Any

import pytest
from fastapi import APIRouter, Depends, FastAPI
from fastapi.testclient import TestClient
from itsdangerous.timed import TimestampSigner
from starlette.requests import Request
from starlette.responses import Response

from mcp_gateway.app import HEALTH_PATH, create_app
from mcp_gateway.bootstrap import Keys
from mcp_gateway.config import ConfigError, Settings, load_settings
from mcp_gateway.mcpsrv.auth import CHALLENGE
from mcp_gateway.mcpsrv.server import mcp_service
from mcp_gateway.web.auth import (
    API_PREFIX,
    BAD_CREDENTIALS,
    HOME_PATH,
    HTMX_REDIRECT,
    HTMX_REQUEST,
    LOGIN_PATH,
    LOGOUT_PATH,
    OPEN_PATHS,
    PROTECTED_PREFIXES,
    SESSION_COOKIE,
    SESSION_MAX_AGE,
    SIGN_IN_REQUIRED,
    UI_PREFIX,
    AdminAuth,
    build_admin,
    require_session,
    safe_next,
    signing_key,
)
from mcp_gateway.web.errors import UNAUTHENTICATED
from mcp_gateway.web.passwords import (
    ALGORITHM,
    PasswordHash,
    PasswordHashInvalid,
    derive,
    parse,
)

USERNAME = "operator"
PASSWORD = "s3cret-password"

#: Derived once, at a cost of one round. These tests are about the plumbing
#: around a hash rather than the hash itself, and 600_000 rounds per login would
#: dominate the suite; the two tests that care about the real cost say so.
CHEAP_HASH = str(derive(PASSWORD, iterations=1))

#: A page behind the guard that nothing mounts. It was ``/ui/servers`` until
#: task 020 made that one real and ``/ui/monitoring`` until task 030 did, so it
#: has moved somewhere no page can follow it to: what is under test is the
#: guard, not the page.
PROTECTED_PAGE = f"{UI_PREFIX}/probe"

#: The same, under the API prefix. It was ``/api/v1/servers`` until task 024
#: made that one real: what is under test is that the prefix is guarded, not
#: what happens to be mounted on it.
PROTECTED_API = f"{API_PREFIX}/probe"

#: What the streamable HTTP transport requires of a POST.
MCP_HEADERS = {
    "content-type": "application/json",
    "accept": "application/json, text/event-stream",
}
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


def locked(tmp_path: Path, extra: str = "") -> Settings:
    """A gateway with admin login configured."""
    return settings_for(
        tmp_path,
        f'[admin]\nusername = "{USERNAME}"\npassword_hash = "{CHEAP_HASH}"\n{extra}',
    )


def admin_for(tmp_path: Path) -> AdminAuth:
    admin = build_admin(locked(tmp_path))
    assert admin is not None
    return admin


def guarded_app(settings: Settings, keys: Keys | None = None, **kwargs: Any) -> FastAPI:
    """An app with a page and an API route standing in for the ones still to come."""
    app = create_app(settings, keys, **kwargs)
    router = APIRouter(dependencies=[Depends(require_session)])

    @router.get(PROTECTED_PAGE)
    async def page(user: Annotated[str | None, Depends(require_session)]) -> dict[str, str | None]:
        return {"user": user}

    @router.get(PROTECTED_API)
    async def api() -> dict[str, bool]:
        return {"ok": True}

    app.include_router(router)
    return app


def sign_in(client: TestClient, password: str = PASSWORD, **form: str) -> Any:
    return client.post(
        LOGIN_PATH,
        data={"username": USERNAME, "password": password, **form},
        follow_redirects=False,
    )


def request_for(scheme: str = "http") -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/",
            "headers": [],
            "scheme": scheme,
            "server": ("testserver", 80),
            "query_string": b"",
        }
    )


def mint(admin: AdminAuth, scheme: str = "http") -> str:
    """A session cookie, made the only way the application makes one."""
    response = Response()
    admin.issue(response, request_for(scheme))
    return SimpleCookie(response.headers["set-cookie"])[SESSION_COOKIE].value


def cookie_flags(response: Any) -> SimpleCookie:
    return SimpleCookie(response.headers["set-cookie"])


# --- the hash ----------------------------------------------------------------


def test_a_derived_hash_verifies_its_own_password() -> None:
    assert derive(PASSWORD, iterations=1).verify(PASSWORD) is True


def test_a_wrong_password_does_not_verify() -> None:
    assert derive(PASSWORD, iterations=1).verify("something else") is False


def test_two_hashes_of_one_password_differ() -> None:
    # Each carries its own random salt, so equal passwords never look equal.
    first = derive(PASSWORD, iterations=1)
    second = derive(PASSWORD, iterations=1)

    assert first.digest != second.digest
    assert second.verify(PASSWORD) is True


def test_the_encoded_form_names_the_algorithm_and_the_cost() -> None:
    encoded = str(derive(PASSWORD, iterations=1234, salt="abcd"))

    assert encoded.split("$")[:3] == [ALGORITHM, "1234", "abcd"]


def test_an_encoded_hash_round_trips() -> None:
    original = derive(PASSWORD, iterations=1)

    assert parse(str(original)) == original


def test_a_stored_hash_verifies_at_the_cost_it_was_written_with() -> None:
    # Raising the default later must not strand hashes written before it.
    restored = parse(str(derive(PASSWORD, iterations=3)))

    assert restored.iterations == 3
    assert restored.verify(PASSWORD) is True


def test_a_hash_never_renders_its_digest() -> None:
    # It is not the password, but it is the thing that accepts one, and this
    # object ends up in tracebacks.
    hashed = derive(PASSWORD, iterations=1)

    assert hashed.digest not in repr(hashed)
    assert "withheld" in repr(hashed)


@pytest.mark.parametrize(
    ("encoded", "reason"),
    [
        ("", "expected"),
        ("pbkdf2_sha256$1$salt", "expected"),
        ("pbkdf2_sha256$1$salt$digest$extra", "expected"),
        ("md5$1$salt$digest", "unsupported algorithm"),
        ("pbkdf2_sha256$lots$salt$digest", "positive integer"),
        ("pbkdf2_sha256$0$salt$digest", "positive integer"),
        ("pbkdf2_sha256$1$$digest", "expected"),
        ("pbkdf2_sha256$1$salt$", "expected"),
    ],
)
def test_a_malformed_hash_says_what_is_wrong_with_it(encoded: str, reason: str) -> None:
    with pytest.raises(PasswordHashInvalid, match=reason):
        parse(encoded)


# --- the account -------------------------------------------------------------


def test_the_configured_credentials_authenticate(tmp_path: Path) -> None:
    assert admin_for(tmp_path).authenticate(USERNAME, PASSWORD) is True


def test_a_wrong_password_does_not_authenticate(tmp_path: Path) -> None:
    assert admin_for(tmp_path).authenticate(USERNAME, "nope") is False


def test_an_unknown_username_does_not_authenticate(tmp_path: Path) -> None:
    assert admin_for(tmp_path).authenticate("someone-else", PASSWORD) is False


def test_the_password_is_hashed_even_when_the_username_is_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Skipping the hash for an unknown account would answer in a fraction of the
    # time, which is how a login form leaks which usernames exist.
    calls: list[str] = []
    original = PasswordHash.verify
    monkeypatch.setattr(
        PasswordHash,
        "verify",
        lambda self, password: (calls.append(password), original(self, password))[1],
    )

    assert admin_for(tmp_path).authenticate("someone-else", PASSWORD) is False
    assert calls == [PASSWORD]


def test_no_admin_section_means_no_account(tmp_path: Path) -> None:
    assert build_admin(settings_for(tmp_path)) is None


def test_a_password_in_the_config_is_hashed_at_startup(tmp_path: Path) -> None:
    # The one place the real iteration count is paid, which is the point: this
    # runs once per process, not once per request.
    settings = settings_for(
        tmp_path, f'[admin]\nusername = "{USERNAME}"\npassword = "{PASSWORD}"\n'
    )
    admin = build_admin(settings)

    assert admin is not None
    assert admin.authenticate(USERNAME, PASSWORD) is True
    assert admin.authenticate(USERNAME, "nope") is False


def test_a_configured_hash_is_used_as_it_stands(tmp_path: Path) -> None:
    admin = admin_for(tmp_path)

    assert admin.authenticate(USERNAME, PASSWORD) is True


def test_a_hash_wins_over_a_password_and_the_choice_is_logged(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    settings = settings_for(
        tmp_path,
        f'[admin]\nusername = "{USERNAME}"\npassword = "the-other-one"\n'
        f'password_hash = "{CHEAP_HASH}"\n',
    )

    with caplog.at_level("WARNING"):
        admin = build_admin(settings)

    assert admin is not None
    assert admin.authenticate(USERNAME, PASSWORD) is True
    assert admin.authenticate(USERNAME, "the-other-one") is False
    assert "using the hash" in caplog.text


def test_a_malformed_hash_stops_the_app_from_being_built(tmp_path: Path) -> None:
    # Better here, loudly, than at the first login attempt, silently.
    settings = settings_for(
        tmp_path, f'[admin]\nusername = "{USERNAME}"\npassword_hash = "not-a-hash"\n'
    )

    with pytest.raises(ConfigError, match=r"admin\.password_hash"):
        create_app(settings)


def test_the_signing_key_comes_from_the_key_file(tmp_path: Path) -> None:
    keys = Keys(secret_key="from-the-key-file", encryption_key="x", path=tmp_path / "keys.json")

    assert signing_key(settings_for(tmp_path), keys) == "from-the-key-file"


def test_a_configured_secret_key_is_used_when_there_is_no_key_file(tmp_path: Path) -> None:
    settings = settings_for(tmp_path, '[security]\nsecret_key = "from-the-config"\n')

    assert signing_key(settings, None) == "from-the-config"


def test_without_any_key_one_is_generated_and_said_out_loud(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level("WARNING"):
        generated = signing_key(settings_for(tmp_path), None)

    assert generated
    assert "will not survive a restart" in caplog.text


# --- the cookie --------------------------------------------------------------


def test_a_cookie_this_gateway_issued_names_its_user(tmp_path: Path) -> None:
    admin = admin_for(tmp_path)

    assert admin.session_user(mint(admin)) == USERNAME


def test_no_cookie_is_nobody(tmp_path: Path) -> None:
    admin = admin_for(tmp_path)

    assert admin.session_user(None) is None
    assert admin.session_user("") is None


def test_a_tampered_cookie_is_refused(tmp_path: Path) -> None:
    admin = admin_for(tmp_path)
    cookie = mint(admin)
    # One character of the payload, which is where the username lives.
    tampered = ("b" if cookie[0] == "a" else "a") + cookie[1:]

    assert admin.session_user(tampered) is None


def test_a_cookie_that_is_not_a_cookie_is_refused(tmp_path: Path) -> None:
    assert admin_for(tmp_path).session_user("garbage") is None


def test_a_cookie_signed_with_another_key_is_refused(tmp_path: Path) -> None:
    hashed = parse(CHEAP_HASH)
    theirs = AdminAuth(USERNAME, hashed, "some-other-secret")
    ours = AdminAuth(USERNAME, hashed, "our-secret")

    assert ours.session_user(mint(theirs)) is None


def test_a_cookie_older_than_its_lifetime_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    admin = admin_for(tmp_path)
    monkeypatch.setattr(
        TimestampSigner,
        "get_timestamp",
        lambda self: int(time.time()) - SESSION_MAX_AGE - 60,
    )
    stale = mint(admin)
    monkeypatch.undo()

    assert admin.session_user(stale) is None


def test_changing_the_password_ends_every_session(tmp_path: Path) -> None:
    # No session table means no way to revoke one; binding the signature to the
    # credentials is what takes its place.
    before = AdminAuth(USERNAME, parse(CHEAP_HASH), "key")
    after = AdminAuth(USERNAME, derive("a-new-password", iterations=1), "key")

    assert after.session_user(mint(before)) is None


def test_changing_the_username_ends_every_session(tmp_path: Path) -> None:
    before = AdminAuth(USERNAME, parse(CHEAP_HASH), "key")
    after = AdminAuth("somebody-else", parse(CHEAP_HASH), "key")

    assert after.session_user(mint(before)) is None


def test_the_cookie_is_marked_secure_only_over_https(tmp_path: Path) -> None:
    # Always-secure would never come back over the documented default
    # deployment — plain HTTP on localhost — and every login would silently
    # bounce straight back to the form.
    admin = admin_for(tmp_path)
    response = Response()

    admin.issue(response, request_for("https"))
    assert cookie_flags(response)[SESSION_COOKIE]["secure"] is True

    response = Response()
    admin.issue(response, request_for("http"))
    assert cookie_flags(response)[SESSION_COOKIE]["secure"] == ""


# --- signing in and out ------------------------------------------------------


def test_the_login_page_renders(tmp_path: Path) -> None:
    with TestClient(create_app(locked(tmp_path))) as client:
        response = client.get(LOGIN_PATH)

    assert response.status_code == 200
    assert 'name="password"' in response.text
    # A cached login page would hand the next person here somebody else's form.
    assert response.headers["cache-control"] == "no-store"


def test_the_login_page_references_nothing_external(tmp_path: Path) -> None:
    # The gateway has to work on an isolated network (spec §7.1).
    with TestClient(create_app(locked(tmp_path))) as client:
        body = client.get(LOGIN_PATH).text

    assert "http://" not in body
    assert "https://" not in body


def test_good_credentials_set_a_session_cookie_with_the_documented_flags(
    tmp_path: Path,
) -> None:
    with TestClient(create_app(locked(tmp_path))) as client:
        response = sign_in(client)

    assert response.status_code == 303
    assert response.headers["location"] == HOME_PATH
    morsel = cookie_flags(response)[SESSION_COOKIE]
    assert morsel["httponly"] is True
    assert morsel["samesite"].lower() == "lax"
    assert morsel["max-age"] == str(SESSION_MAX_AGE)
    assert morsel["path"] == "/"


@pytest.mark.parametrize("username", [USERNAME, "somebody-who-does-not-exist"])
def test_bad_credentials_are_refused_with_one_message(tmp_path: Path, username: str) -> None:
    # A wrong password and an unknown account are the same answer, word for word.
    with TestClient(create_app(locked(tmp_path))) as client:
        response = client.post(
            LOGIN_PATH,
            data={"username": username, "password": "wrong"},
            follow_redirects=False,
        )

    assert response.status_code == 401
    assert BAD_CREDENTIALS in response.text
    assert SESSION_COOKIE not in response.cookies


def test_a_failed_login_leaves_the_username_in_the_form(tmp_path: Path) -> None:
    with TestClient(create_app(locked(tmp_path))) as client:
        response = sign_in(client, password="wrong")

    assert f'value="{USERNAME}"' in response.text


def test_signing_in_returns_to_where_the_caller_was_going(tmp_path: Path) -> None:
    with TestClient(create_app(locked(tmp_path))) as client:
        response = sign_in(client, next=f"{UI_PREFIX}/monitoring")

    assert response.headers["location"] == f"{UI_PREFIX}/monitoring"


@pytest.mark.parametrize(
    "target",
    ["https://evil.example/", "//evil.example/", "/\\evil.example/", "not-a-path", ""],
)
def test_a_next_that_could_leave_the_gateway_is_dropped(target: str) -> None:
    # ``next`` arrives in a link somebody clicked, so it is attacker-supplied by
    # definition. Both ``//host`` and ``/\host`` are absolute in a browser.
    assert safe_next(target) == HOME_PATH


def test_an_offsite_next_is_dropped_over_the_wire(tmp_path: Path) -> None:
    with TestClient(create_app(locked(tmp_path))) as client:
        response = sign_in(client, next="https://evil.example/")

    assert response.headers["location"] == HOME_PATH


def test_the_login_page_sends_an_already_signed_in_caller_onwards(tmp_path: Path) -> None:
    with TestClient(create_app(locked(tmp_path))) as client:
        sign_in(client)
        response = client.get(LOGIN_PATH, follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == HOME_PATH


def test_signing_out_clears_the_cookie_and_the_session(tmp_path: Path) -> None:
    with TestClient(guarded_app(locked(tmp_path))) as client:
        sign_in(client)
        assert client.get(PROTECTED_PAGE).status_code == 200

        response = client.post(LOGOUT_PATH, follow_redirects=False)

        assert response.status_code == 303
        assert response.headers["location"] == LOGIN_PATH
        assert client.get(PROTECTED_PAGE, follow_redirects=False).status_code == 303


# --- the guard ---------------------------------------------------------------


def test_a_signed_in_caller_reaches_a_protected_page(tmp_path: Path) -> None:
    with TestClient(guarded_app(locked(tmp_path))) as client:
        sign_in(client)
        response = client.get(PROTECTED_PAGE)

    assert response.status_code == 200
    # The guard hands the handler the user it recognised.
    assert response.json() == {"user": USERNAME}


def test_an_anonymous_browser_is_sent_to_the_login_page(tmp_path: Path) -> None:
    with TestClient(guarded_app(locked(tmp_path))) as client:
        response = client.get(PROTECTED_PAGE, follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == f"{LOGIN_PATH}?next=%2Fui%2Fprobe"


def test_an_anonymous_api_request_gets_401_rather_than_a_redirect(tmp_path: Path) -> None:
    # A script following a redirect would read a login form as its result and
    # see a success where there was none (task 024).
    with TestClient(guarded_app(locked(tmp_path))) as client:
        response = client.get(PROTECTED_API, follow_redirects=False)

    assert response.status_code == 401
    # The envelope every API failure wears (task 024), not a redirect and not a
    # shape of its own.
    assert response.json() == {
        "status": 401,
        "code": UNAUTHENTICATED,
        "message": SIGN_IN_REQUIRED,
        "fields": {},
    }


def test_an_anonymous_htmx_request_is_told_to_navigate(tmp_path: Path) -> None:
    # htmx follows a redirect itself and would swap the whole login page into
    # whatever fragment was being updated.
    with TestClient(guarded_app(locked(tmp_path))) as client:
        response = client.get(PROTECTED_PAGE, headers={HTMX_REQUEST: "true"})

    assert response.status_code == 401
    assert response.headers[HTMX_REDIRECT] == f"{LOGIN_PATH}?next=%2Fui%2Fprobe"


def test_a_tampered_cookie_does_not_open_a_protected_page(tmp_path: Path) -> None:
    app = guarded_app(locked(tmp_path))
    with TestClient(app) as client:
        sign_in(client)
        good = client.cookies[SESSION_COOKIE]
        client.cookies.set(SESSION_COOKIE, ("b" if good[0] == "a" else "a") + good[1:])

        response = client.get(PROTECTED_PAGE, follow_redirects=False)

    assert response.status_code == 303


def test_an_invented_cookie_does_not_open_a_protected_page(tmp_path: Path) -> None:
    with TestClient(guarded_app(locked(tmp_path))) as client:
        client.cookies.set(SESSION_COOKIE, USERNAME)

        assert client.get(PROTECTED_API, follow_redirects=False).status_code == 401


def test_every_route_under_a_protected_prefix_carries_the_guard(tmp_path: Path) -> None:
    # Vacuous today and deliberately so: tasks 019 to 024 add the routes, and this
    # is what stops one of them from arriving without the dependency.
    app = create_app(locked(tmp_path))

    unguarded = []
    for route in app.routes:
        path = str(getattr(route, "path", ""))
        if not path.startswith(PROTECTED_PREFIXES) or path in OPEN_PATHS:
            continue
        dependant = getattr(route, "dependant", None)
        if dependant is None or not any(
            dependency.call is require_session for dependency in dependant.dependencies
        ):
            unguarded.append(path)

    assert unguarded == []


# --- the open mode -----------------------------------------------------------


def test_an_open_gateway_mounts_no_login_route(tmp_path: Path) -> None:
    # Not a login that always succeeds: there is nothing there to post to.
    with TestClient(guarded_app(settings_for(tmp_path))) as client:
        assert client.get(LOGIN_PATH).status_code == 404
        assert client.post(LOGIN_PATH, data={"username": "a", "password": "b"}).status_code == 404
        assert client.post(LOGOUT_PATH).status_code == 404


def test_an_open_gateway_leaves_the_protected_routes_reachable(tmp_path: Path) -> None:
    with TestClient(guarded_app(settings_for(tmp_path))) as client:
        page = client.get(PROTECTED_PAGE)
        api = client.get(PROTECTED_API)

    assert page.status_code == 200
    assert page.json() == {"user": None}
    assert api.status_code == 200


def test_an_open_gateway_has_no_account(tmp_path: Path) -> None:
    assert create_app(settings_for(tmp_path)).state.admin is None


# --- the two authentication systems ------------------------------------------


@pytest.mark.parametrize("configure", [locked, settings_for])
def test_healthz_is_open_in_both_modes(tmp_path: Path, configure: Any) -> None:
    with TestClient(guarded_app(configure(tmp_path))) as client:
        response = client.get(HEALTH_PATH, follow_redirects=False)

    assert response.status_code == 200


@pytest.mark.parametrize("configure", [locked, settings_for])
def test_mcp_answers_the_same_in_both_modes(tmp_path: Path, configure: Any) -> None:
    app = guarded_app(configure(tmp_path), services=[mcp_service])

    with TestClient(app) as client:
        response = client.post("/mcp", headers=MCP_HEADERS, json=HANDSHAKE)

    assert response.status_code == 200


def test_an_admin_session_does_not_open_a_token_protected_mcp(tmp_path: Path) -> None:
    # The mirror of the same boundary from the other side (task 017): being
    # signed in to the UI says nothing about being allowed to call the tools.
    settings = locked(tmp_path, extra='\n[mcp]\nauth_token = "a-long-random-string"\n')
    app = create_app(settings, services=[mcp_service])

    with TestClient(app) as client:
        sign_in(client)
        response = client.post("/mcp", headers=MCP_HEADERS, json=HANDSHAKE)

    assert client.cookies.get(SESSION_COOKIE) is not None
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == CHALLENGE
