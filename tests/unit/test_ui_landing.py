"""The front door: ``/``, ``/ui`` and ``/ui/``.

Spec §7.1, task 109. Three addresses that used to answer *404 Nothing here* —
including the one the startup banner prints — and the one route that now points
them all at the page the UI starts on, without getting in front of a gateway
that serves MCP at ``/``.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from mcp_gateway.app import HEALTH_PATH, create_app
from mcp_gateway.config import Settings, load_settings
from mcp_gateway.db.session import database_service
from mcp_gateway.web.auth import HOME_PATH, LOGIN_PATH, UI_PREFIX
from mcp_gateway.web.landing import LANDING_PATHS, LANDING_STATUS, ROOT_PATH
from mcp_gateway.web.routes_api import HEALTH_PATH as API_HEALTH_PATH
from mcp_gateway.web.shell import STATIC_PREFIX

HTML = {"accept": "text/html,application/xhtml+xml"}

#: What a login round-trip from the landing page has to carry.
NEXT_TO_THE_LIST = f"{LOGIN_PATH}?next=%2Fui%2Fservers"

#: A redirect the browser would remember for good. The whole point of the
#: status chosen in ``landing`` is not being one of these.
PERMANENT = (301, 308)


def settings_for(tmp_path: Path, body: str = "") -> Settings:
    config = tmp_path / "config.toml"
    config.write_text(body, encoding="utf-8")
    return load_settings({"config": str(config)}, environ={})


def locked(tmp_path: Path) -> Settings:
    return settings_for(tmp_path, '[admin]\nusername = "operator"\npassword = "s3cret"\n')


def client(settings: Settings) -> TestClient:
    """An app with a database, so the page at the end of the redirect renders."""
    return TestClient(create_app(settings, services=[database_service(settings)]))


# --- where the three addresses go --------------------------------------------


@pytest.mark.parametrize("path", LANDING_PATHS)
def test_a_landing_address_opens_the_server_list(tmp_path: Path, path: str) -> None:
    with client(settings_for(tmp_path)) as http:
        response = http.get(path, follow_redirects=False)

    assert response.status_code == LANDING_STATUS
    assert response.headers["location"] == HOME_PATH


@pytest.mark.parametrize("path", LANDING_PATHS)
def test_a_landing_address_opens_the_list_with_a_login_configured_too(
    tmp_path: Path, path: str
) -> None:
    # The redirect itself is open: it is the page it points at that guards
    # itself, so the operator meets the login once rather than twice.
    with client(locked(tmp_path)) as http:
        response = http.get(path, follow_redirects=False)

    assert response.status_code == LANDING_STATUS
    assert response.headers["location"] == HOME_PATH


@pytest.mark.parametrize("path", LANDING_PATHS)
def test_the_redirect_is_temporary(tmp_path: Path, path: str) -> None:
    with client(settings_for(tmp_path)) as http:
        response = http.get(path, follow_redirects=False)

    assert response.status_code not in PERMANENT


def test_the_root_lands_on_the_login_and_signing_in_arrives_at_the_list(tmp_path: Path) -> None:
    settings = locked(tmp_path)

    with client(settings) as http:
        arrived = http.get(ROOT_PATH, follow_redirects=True)
        signed_in = http.post(
            LOGIN_PATH,
            data={"username": "operator", "password": "s3cret", "next": HOME_PATH},
            follow_redirects=True,
        )

    assert arrived.status_code == 200
    assert arrived.url.path == LOGIN_PATH
    assert arrived.url.query.decode() == "next=%2Fui%2Fservers"
    assert signed_in.status_code == 200
    assert signed_in.url.path == HOME_PATH


# --- what the landing route must not stand in front of -----------------------


def test_a_root_mounted_mcp_endpoint_is_not_shadowed(tmp_path: Path) -> None:
    # ``mcp.path = "/"`` is a reachable configuration, and a client pointed at
    # it must get MCP rather than a redirect to a login page. 503 is what the
    # endpoint answers in an app whose services were never started, which is
    # exactly the point: the request reached the endpoint.
    settings = settings_for(tmp_path, '[mcp]\npath = "/"\n')

    with client(settings) as http:
        got = http.get(ROOT_PATH, follow_redirects=False)
        posted = http.post(ROOT_PATH, follow_redirects=False)
        section = http.get(UI_PREFIX, follow_redirects=False)

    assert got.status_code == 503
    assert posted.status_code == 503
    # The other two addresses collide with nothing and still work.
    assert section.status_code == LANDING_STATUS


def test_the_default_mcp_path_is_untouched(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)

    with client(settings) as http:
        assert http.get(settings.mcp.path, follow_redirects=False).status_code == 503
        assert http.get(ROOT_PATH, follow_redirects=False).status_code == LANDING_STATUS


def test_everything_else_at_the_root_answers_what_it_did(tmp_path: Path) -> None:
    with client(settings_for(tmp_path)) as http:
        assert http.get(HEALTH_PATH).status_code == 200
        assert http.get(API_HEALTH_PATH).status_code == 200
        assert http.get(f"{STATIC_PREFIX}/css/app.css").status_code == 200


def test_an_address_that_matches_nothing_still_reaches_the_404_page(tmp_path: Path) -> None:
    # The 404 page's wording is about a bookmark to a server that was deleted,
    # which is what it is for; this task took ``/`` away from it, not the page.
    with client(settings_for(tmp_path)) as http:
        page = http.get("/nowhere", headers=HTML)
        api = http.get("/nowhere")

    assert page.status_code == 404
    assert "Nothing here" in page.text
    assert api.status_code == 404
    assert api.headers["content-type"].startswith("application/json")
