"""The UI shell: the layout, the navigation, the assets, flashes, error pages.

Spec §7.1, task 019.
"""

from __future__ import annotations

import re
import time
from http.cookies import SimpleCookie
from pathlib import Path
from typing import Any

import pytest
from fastapi import APIRouter, FastAPI, Request
from fastapi.testclient import TestClient
from itsdangerous.timed import TimestampSigner
from starlette.responses import RedirectResponse, Response

import mcp_gateway
from mcp_gateway.app import HEALTH_PATH, create_app
from mcp_gateway.config import Settings, load_settings
from mcp_gateway.web.auth import API_PREFIX, HOME_PATH, LOGIN_PATH, UI_PREFIX
from mcp_gateway.web.shell import (
    ERROR_PAGES,
    FLASH_COOKIE,
    FLASH_MAX_AGE,
    MAX_FLASH_CHARS,
    MAX_FLASHES,
    MONITORING_PATH,
    NAV,
    STATIC_DIR,
    STATIC_PREFIX,
    TEMPLATES_DIR,
    Flash,
    Shell,
    active_item,
    static_url,
    under,
)

#: A page that renders nothing but the layout. Task 020 owns ``/ui/servers`` and
#: task 023 owns ``/ui/servers/{id}``, so the stand-in has moved down again — it
#: only has to be somewhere under ``/ui/servers``, which is what makes the
#: masthead light Configuration up.
LAYOUT_PAGE = f"{UI_PREFIX}/servers/7/layout"
BOOM_PAGE = f"{UI_PREFIX}/boom"
FLASH_PAGE = f"{UI_PREFIX}/flash"

HTML = {"accept": "text/html,application/xhtml+xml"}

#: Anything in a page that would send the browser off this gateway. ``//host``
#: counts: it is an absolute reference to another host despite the leading slash.
EXTERNAL = re.compile(r"""(?:https?:)?//[^/"'\s]""")

#: The same question asked of a vendored script, where a bare ``//`` is a comment
#: rather than a URL and only a scheme means anything.
EXTERNAL_IN_ASSET = re.compile(r"https?://")

#: Every file the browser can load from this package. Discovered rather than
#: listed, so an asset added later is held to the same two rules without anybody
#: having to remember to add it here.
ASSETS = sorted(p.relative_to(STATIC_DIR).as_posix() for p in STATIC_DIR.rglob("*") if p.is_file())


def settings_for(tmp_path: Path, body: str = "") -> Settings:
    config = tmp_path / "config.toml"
    config.write_text(body, encoding="utf-8")
    return load_settings({"config": str(config)}, environ={})


def locked(tmp_path: Path) -> Settings:
    return settings_for(tmp_path, '[admin]\nusername = "operator"\npassword = "s3cret"\n')


def shelled_app(settings: Settings, **kwargs: Any) -> FastAPI:
    """An app with a page of its own that renders nothing but the layout.

    They render ``base.html`` directly: the shell is what is under test, and a
    page with nothing in its content block is exactly the layout.
    """
    app = create_app(settings, **kwargs)
    router = APIRouter()

    @router.get(f"{UI_PREFIX}/servers/{{server_id}}/layout")
    @router.get(MONITORING_PATH)
    async def page(request: Request) -> Response:
        shell: Shell = request.app.state.shell
        return shell.render(request, "base.html")

    @router.get(FLASH_PAGE)
    async def leaves_a_flash(request: Request) -> Response:
        shell: Shell = request.app.state.shell
        response = RedirectResponse(LAYOUT_PAGE, status_code=303)
        for level, message in request.query_params.multi_items():
            shell.flash(request, response, message, level=level)  # type: ignore[arg-type]
        return response

    @router.get(BOOM_PAGE)
    async def boom() -> Response:
        raise RuntimeError("the upstream ate the request")

    app.include_router(router)
    return app


def client(settings: Settings, **kwargs: Any) -> TestClient:
    # Server exceptions are answered rather than re-raised, which is what a real
    # uvicorn does and the only way to see the 500 page at all.
    return TestClient(shelled_app(settings, **kwargs), raise_server_exceptions=False)


def cookie_flags(response: Any, name: str) -> Any:
    return SimpleCookie(response.headers["set-cookie"])[name]


# --- the layout --------------------------------------------------------------


def test_a_page_renders_into_the_layout(tmp_path: Path) -> None:
    with client(settings_for(tmp_path)) as http:
        body = http.get(LAYOUT_PAGE, headers=HTML).text

    assert "<!doctype html>" in body
    assert 'class="masthead"' in body
    assert 'class="page"' in body
    assert f"mcp-gateway {mcp_gateway.__version__}" in body


def test_the_layout_loads_the_stylesheet_and_htmx(tmp_path: Path) -> None:
    with client(settings_for(tmp_path)) as http:
        body = http.get(LAYOUT_PAGE, headers=HTML).text

    assert static_url("css/app.css") in body
    assert static_url("js/htmx.min.js") in body
    # And how this gateway answers htmx, which every page that uses it needs.
    assert static_url("js/gateway.js") in body
    assert body.index("htmx.min.js") < body.index("gateway.js")


def test_an_asset_url_carries_the_release_that_shipped_it() -> None:
    # What lets the assets be cached hard and still change on an upgrade.
    assert static_url("css/app.css") == f"{STATIC_PREFIX}/css/app.css?v={mcp_gateway.__version__}"


def test_values_are_escaped_rather_than_trusted(tmp_path: Path) -> None:
    with client(settings_for(tmp_path)) as http:
        body = http.get(FLASH_PAGE, params={"info": "<script>alert(1)</script>"}).text

    assert "<script>alert(1)</script>" not in body
    assert "&lt;script&gt;" in body


def test_the_signed_out_masthead_offers_no_sign_out(tmp_path: Path) -> None:
    # Open mode: there is nobody to sign out, and no logout route to point at.
    with client(settings_for(tmp_path)) as http:
        assert "Sign out" not in http.get(LAYOUT_PAGE, headers=HTML).text


def test_a_signed_in_masthead_offers_a_sign_out(tmp_path: Path) -> None:
    with client(locked(tmp_path)) as http:
        http.post(LOGIN_PATH, data={"username": "operator", "password": "s3cret"})
        assert "Sign out" in http.get(LAYOUT_PAGE, headers=HTML).text


# --- the navigation ----------------------------------------------------------


def test_the_nav_names_both_sections(tmp_path: Path) -> None:
    with client(settings_for(tmp_path)) as http:
        body = http.get(LAYOUT_PAGE, headers=HTML).text

    for item in NAV:
        assert f'href="{item.path}"' in body
        assert item.label in body


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        (HOME_PATH, "Configuration"),
        (f"{HOME_PATH}/new", "Configuration"),
        (f"{HOME_PATH}/7", "Configuration"),
        (MONITORING_PATH, "Monitoring"),
        (f"{MONITORING_PATH}/anything", "Monitoring"),
    ],
)
def test_a_path_belongs_to_its_section(path: str, expected: str) -> None:
    item = active_item(path)
    assert item is not None
    assert item.label == expected


@pytest.mark.parametrize("path", [LOGIN_PATH, "/ui", "/healthz", "/", "/ui/serversextra"])
def test_a_path_outside_the_sections_lights_none_of_them(path: str) -> None:
    assert active_item(path) is None


def test_the_active_section_is_marked_in_the_page(tmp_path: Path) -> None:
    with client(settings_for(tmp_path)) as http:
        body = http.get(MONITORING_PATH, headers=HTML).text

    marked = re.findall(r'<a\s+class="nav__item nav__item--active"\s+href="([^"]+)"', body)
    assert marked == [MONITORING_PATH]
    assert 'aria-current="page"' in body


def test_a_child_page_still_marks_its_section(tmp_path: Path) -> None:
    with client(settings_for(tmp_path)) as http:
        body = http.get(LAYOUT_PAGE, headers=HTML).text

    marked = re.findall(r'<a\s+class="nav__item nav__item--active"\s+href="([^"]+)"', body)
    assert marked == [HOME_PATH]


def test_the_login_page_marks_no_section(tmp_path: Path) -> None:
    with client(locked(tmp_path)) as http:
        assert "nav__item--active" not in http.get(LOGIN_PATH, headers=HTML).text


@pytest.mark.parametrize(
    ("path", "prefix", "expected"),
    [
        ("/ui/servers", "/ui/servers", True),
        ("/ui/servers/7", "/ui/servers", True),
        ("/ui/serversextra", "/ui/servers", False),
        ("/ui", "/ui/servers", False),
        # A gateway that mounted MCP at the root has nothing that is not MCP.
        ("/anything", "/", True),
    ],
)
def test_what_counts_as_being_under_a_prefix(path: str, prefix: str, expected: bool) -> None:
    assert under(path, prefix) is expected


# --- the vendored assets -----------------------------------------------------


def test_htmx_is_vendored_into_the_package() -> None:
    htmx = STATIC_DIR / "js" / "htmx.min.js"

    assert htmx.is_file()
    assert htmx.stat().st_size > 10_000


@pytest.mark.parametrize("asset", ASSETS)
def test_an_asset_is_served(tmp_path: Path, asset: str) -> None:
    with client(settings_for(tmp_path)) as http:
        response = http.get(f"{STATIC_PREFIX}/{asset}")

    assert response.status_code == 200
    assert response.content


def test_assets_load_without_a_session(tmp_path: Path) -> None:
    # The stylesheet has to render the login page, which by definition happens
    # to someone who is not signed in (spec §3.3).
    with client(locked(tmp_path)) as http:
        assert http.get(f"{STATIC_PREFIX}/css/app.css").status_code == 200


@pytest.mark.parametrize(
    "template",
    sorted(p.relative_to(TEMPLATES_DIR).as_posix() for p in TEMPLATES_DIR.rglob("*.html")),
)
def test_no_template_refers_to_another_host(template: str) -> None:
    # The gateway has to work on a network with no route to a CDN (spec §7.1).
    source = (TEMPLATES_DIR / template).read_text(encoding="utf-8")

    assert EXTERNAL.search(source) is None, f"{template} refers to another host"


@pytest.mark.parametrize("asset", ASSETS)
def test_no_asset_refers_to_another_host(asset: str) -> None:
    source = (STATIC_DIR / asset).read_text(encoding="utf-8")

    assert EXTERNAL_IN_ASSET.search(source) is None


@pytest.mark.parametrize("path", [LAYOUT_PAGE, MONITORING_PATH])
def test_no_rendered_page_refers_to_another_host(tmp_path: Path, path: str) -> None:
    with client(locked(tmp_path)) as http:
        body = http.get(path, headers=HTML).text

    assert EXTERNAL.search(body) is None


# --- flash messages ----------------------------------------------------------


def test_a_flash_survives_the_redirect_that_set_it(tmp_path: Path) -> None:
    with client(settings_for(tmp_path)) as http:
        response = http.get(FLASH_PAGE, params={"success": "Server saved."})

    assert response.status_code == 200
    assert "Server saved." in response.text
    assert 'class="flash flash--success"' in response.text


def test_a_flash_is_shown_once(tmp_path: Path) -> None:
    with client(settings_for(tmp_path)) as http:
        http.get(FLASH_PAGE, params={"info": "Only once."})
        assert "Only once." not in http.get(LAYOUT_PAGE, headers=HTML).text


def test_showing_a_flash_clears_its_cookie(tmp_path: Path) -> None:
    with client(settings_for(tmp_path)) as http:
        http.get(FLASH_PAGE, params={"info": "Gone after this."}, follow_redirects=False)
        assert http.cookies.get(FLASH_COOKIE)
        http.get(LAYOUT_PAGE, headers=HTML)

    assert http.cookies.get(FLASH_COOKIE) is None


def test_several_flashes_all_arrive(tmp_path: Path) -> None:
    with client(settings_for(tmp_path)) as http:
        body = http.get(FLASH_PAGE, params={"success": "Saved.", "warning": "Two are new."}).text

    assert "Saved." in body
    assert "Two are new." in body


def test_only_the_last_few_flashes_are_kept(tmp_path: Path) -> None:
    # The cookie has to stay under the 4 KB a browser will store.
    messages = [f"message {n}" for n in range(MAX_FLASHES + 3)]
    with client(settings_for(tmp_path)) as http:
        body = http.get(FLASH_PAGE, params=[("info", m) for m in messages]).text

    assert messages[0] not in body
    assert messages[-1] in body
    assert body.count('class="flash flash--') == MAX_FLASHES


def test_a_long_flash_is_truncated(tmp_path: Path) -> None:
    with client(settings_for(tmp_path)) as http:
        body = http.get(FLASH_PAGE, params={"info": "x" * (MAX_FLASH_CHARS * 2)}).text

    assert "x" * MAX_FLASH_CHARS in body
    assert "x" * (MAX_FLASH_CHARS + 1) not in body


def test_an_unknown_level_falls_back_to_info(tmp_path: Path) -> None:
    with client(settings_for(tmp_path)) as http:
        body = http.get(FLASH_PAGE, params={"shouting": "Hello."}).text

    assert 'class="flash flash--info"' in body


def test_the_flash_cookie_is_not_readable_by_a_script(tmp_path: Path) -> None:
    with client(settings_for(tmp_path)) as http:
        response = http.get(FLASH_PAGE, params={"info": "hi"}, follow_redirects=False)

    cookie = cookie_flags(response, FLASH_COOKIE)
    assert cookie["httponly"]
    assert cookie["samesite"].lower() == "lax"
    assert cookie["path"] == "/"
    assert int(cookie["max-age"]) == FLASH_MAX_AGE


def test_a_tampered_flash_cookie_is_ignored(tmp_path: Path) -> None:
    with client(settings_for(tmp_path)) as http:
        http.get(FLASH_PAGE, params={"info": "Trusted."}, follow_redirects=False)
        signed = http.cookies[FLASH_COOKIE]
        http.cookies.set(FLASH_COOKIE, signed[:-4] + "AAAA")
        body = http.get(LAYOUT_PAGE, headers=HTML).text

    assert "Trusted." not in body
    assert 'class="flash flash--' not in body


def test_an_invented_flash_cookie_is_ignored(tmp_path: Path) -> None:
    with client(settings_for(tmp_path)) as http:
        http.cookies.set(FLASH_COOKIE, "not-a-signed-value")
        assert http.get(LAYOUT_PAGE, headers=HTML).status_code == 200


def test_a_stale_flash_is_dropped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    shell = Shell("a-key")
    monkeypatch.setattr(
        TimestampSigner, "get_timestamp", lambda self: int(time.time()) - FLASH_MAX_AGE - 60
    )
    response = Response()
    request = _request_with_no_cookies()
    shell.flash(request, response, "Ancient news.")
    monkeypatch.undo()
    stale = SimpleCookie(response.headers["set-cookie"])[FLASH_COOKIE].value

    assert shell.take(_request_with_cookie(stale)) == []


def test_a_payload_of_the_wrong_shape_is_dropped() -> None:
    # A cookie signed by an older release, whose flashes were shaped differently.
    shell = Shell("a-key")
    signed = shell._signer.dumps({"level": "info", "message": "old shape"})

    assert shell.take(_request_with_cookie(signed)) == []


def test_a_flash_cookie_signed_with_another_key_is_ignored() -> None:
    theirs = Shell("their-key")
    response = Response()
    theirs.flash(_request_with_no_cookies(), response, "Not from here.")
    forged = SimpleCookie(response.headers["set-cookie"])[FLASH_COOKIE].value

    assert Shell("our-key").take(_request_with_cookie(forged)) == []


def _request_with_no_cookies() -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/",
            "headers": [],
            "scheme": "http",
            "server": ("testserver", 80),
            "query_string": b"",
        }
    )


def _request_with_cookie(value: str) -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/",
            "headers": [(b"cookie", f"{FLASH_COOKIE}={value}".encode())],
            "scheme": "http",
            "server": ("testserver", 80),
            "query_string": b"",
        }
    )


# --- error pages -------------------------------------------------------------


def test_the_404_page_renders(tmp_path: Path) -> None:
    with client(settings_for(tmp_path)) as http:
        response = http.get("/no-such-page", headers=HTML)

    assert response.status_code == 404
    assert response.headers["content-type"].startswith("text/html")
    assert '<p class="error__status">404</p>' in response.text
    assert "Nothing here" in response.text


def test_the_401_page_renders(tmp_path: Path) -> None:
    shell = Shell("a-key")
    with client(locked(tmp_path)) as http:
        request = _request_with_no_cookies()
        request.scope["app"] = http.app
        body = shell.error_page(request, 401).body.decode()

    assert "Sign in to continue" in body
    assert f'href="{LOGIN_PATH}"' in body


def test_the_500_page_renders(tmp_path: Path) -> None:
    with client(settings_for(tmp_path)) as http:
        response = http.get(BOOM_PAGE, headers=HTML)

    assert response.status_code == 500
    assert "The gateway hit an error" in response.text
    # Never the exception text: the operator reads the log, the browser does not.
    assert "the upstream ate the request" not in response.text


def test_every_documented_status_has_a_page_of_its_own() -> None:
    for status, template in ERROR_PAGES.items():
        assert (TEMPLATES_DIR / template).is_file()
        assert str(status) in template


def test_an_undocumented_status_gets_the_generic_page(tmp_path: Path) -> None:
    shell = Shell("a-key")
    with client(settings_for(tmp_path)) as http:
        request = _request_with_no_cookies()
        request.scope["app"] = http.app
        response = shell.error_page(request, 418)

    assert response.status_code == 418
    assert "Something went wrong" in response.body.decode()


def test_a_broken_error_page_still_answers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # The error page is the last thing between a failure and the operator.
    shell = Shell("a-key")
    monkeypatch.setattr(Shell, "render", _explode)
    with caplog.at_level("ERROR"):
        response = shell.error_page(_request_with_no_cookies(), 500)

    assert response.status_code == 500
    assert response.body == b"Error 500"
    assert "could not be rendered" in caplog.text


def _explode(*args: Any, **kwargs: Any) -> Response:
    raise RuntimeError("the template is broken too")


def test_a_missing_api_route_answers_in_json(tmp_path: Path) -> None:
    # A script following an HTML page would read a form as its result (task 024).
    with client(settings_for(tmp_path)) as http:
        response = http.get(f"{API_PREFIX}/nothing", headers=HTML)

    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/json")


def test_a_caller_that_did_not_ask_for_html_gets_json(tmp_path: Path) -> None:
    with client(settings_for(tmp_path)) as http:
        response = http.get("/no-such-page", headers={"accept": "*/*"})

    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/json")


def test_the_mcp_endpoint_never_answers_with_a_page(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    with client(settings) as http:
        response = http.get(f"{settings.mcp.path}/nothing", headers=HTML)

    assert response.headers["content-type"].startswith("application/json")


def test_healthz_is_untouched_by_the_shell(tmp_path: Path) -> None:
    with client(settings_for(tmp_path)) as http:
        response = http.get(HEALTH_PATH, headers=HTML)

    assert response.status_code == 200
    assert response.json()["status"] == "ok"


# --- the shared partials -----------------------------------------------------


def render_snippet(source: str, **context: Any) -> str:
    return Shell("a-key").templates.env.from_string(source).render(**context)


def test_the_status_badge_names_a_known_status() -> None:
    body = render_snippet(
        '{% from "partials/status_badge.html" import status_badge %}{{ status_badge("attention") }}'
    )

    assert 'class="badge badge--attention"' in body
    assert "Needs attention" in body


def test_the_status_badge_falls_back_to_the_status_it_was_given() -> None:
    body = render_snippet(
        '{% from "partials/status_badge.html" import status_badge %}{{ status_badge("draining") }}'
    )

    assert ">draining<" in body


def test_the_status_badge_takes_a_label_and_a_tooltip() -> None:
    body = render_snippet(
        '{% from "partials/status_badge.html" import status_badge %}'
        '{{ status_badge("error", label="3 failures", title="Last: 502") }}'
    )

    assert ">3 failures<" in body
    assert 'title="Last: 502"' in body


def test_the_empty_state_can_point_at_what_fixes_it() -> None:
    body = render_snippet(
        '{% from "partials/empty_state.html" import empty_state %}'
        '{{ empty_state("No servers yet", "Register a service.",'
        ' href="/ui/servers/new", action="Add a server") }}'
    )

    assert "No servers yet" in body
    assert 'href="/ui/servers/new"' in body
    assert ">Add a server<" in body


def test_the_empty_state_without_an_action_renders_no_button() -> None:
    body = render_snippet(
        '{% from "partials/empty_state.html" import empty_state %}'
        '{{ empty_state("Nothing in range", "No calls were recorded.") }}'
    )

    assert "Nothing in range" in body
    assert "<a" not in body


def test_the_confirm_button_asks_before_it_acts() -> None:
    body = render_snippet(
        '{% from "partials/confirm.html" import confirm %}'
        '{{ confirm("/api/v1/servers/7", "Delete", "Delete petstore?") }}'
    )

    assert 'hx-delete="/api/v1/servers/7"' in body
    assert 'hx-confirm="Delete petstore?"' in body
    assert ">Delete<" in body


def test_the_confirm_button_can_use_another_method() -> None:
    body = render_snippet(
        '{% from "partials/confirm.html" import confirm %}'
        '{{ confirm("/api/v1/servers/7/refresh", "Refresh", "Refresh now?", method="post") }}'
    )

    assert 'hx-post="/api/v1/servers/7/refresh"' in body


def test_a_partial_escapes_what_it_is_given() -> None:
    body = render_snippet(
        '{% from "partials/status_badge.html" import status_badge %}'
        '{{ status_badge("error", label=evil) }}',
        evil="<script>alert(1)</script>",
    )

    assert "<script>" not in body


def test_a_flash_named_tuple_reads_by_name() -> None:
    # Templates address these by attribute; a plain tuple would render nothing.
    flash = Flash("warning", "Two operations are new.")

    assert flash.level == "warning"
    assert flash.message == "Two operations are new."
