"""Reading an upstream MCP server: the preview, the credential, and every way to fail.

The upstream is a small Streamable HTTP server written here — ``initialize``,
``notifications/initialized`` and ``tools/list``, answered as JSON — and stood
behind an ASGI transport, so that no socket is involved and every reply can be
made wrong in exactly one way. The SDK's client transport is real; only the
network under it is not. ``tests/integration/test_mcp_endpoint.py`` covers the
socket, against the gateway's own endpoint.

Every credential in this file starts with ``SENTINEL-``, so one test can collect
what the module produces when things go wrong and prove none of it carries a
token.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import httpx2
import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import (
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from starlette.routing import Route

from mcp_gateway import __version__
from mcp_gateway.config import HttpSettings
from mcp_gateway.crypto import (
    ApiKeyCredential,
    BasicCredential,
    BearerCredential,
    Credential,
    HeadersCredential,
)
from mcp_gateway.mcpclient import (
    EndpointError,
    EndpointNetworkError,
    EndpointNoToolsError,
    EndpointProtocolError,
    EndpointStatusError,
    EndpointTooLargeError,
    open_session,
    preview_endpoint,
)
from mcp_gateway.mcpclient.connect import CLIENT_NAME
from mcp_gateway.mcpclient.preview import MAX_PAGES

ENDPOINT = "http://files.example/mcp"

TOKEN = "SENTINEL-TOKEN"
KEY = "SENTINEL-KEY"
PASSWORD = "SENTINEL-PASSWORD"
HEADER_VALUE = "SENTINEL-HEADER"
SECRETS = (TOKEN, KEY, PASSWORD, HEADER_VALUE)

BEARER = BearerCredential(token=TOKEN)  # type: ignore[arg-type]
API_KEY = ApiKeyCredential(header="X-Api-Key", value=KEY)  # type: ignore[arg-type]
BASIC = BasicCredential(username="reader", password=PASSWORD)  # type: ignore[arg-type]
HEADERS = HeadersCredential(headers={"X-Team": HEADER_VALUE})  # type: ignore[arg-type]

#: What each credential looks like from the upstream's side.
PRESENTED: dict[str, tuple[str, str]] = {
    "bearer": ("authorization", f"Bearer {TOKEN}"),
    "api_key": ("x-api-key", KEY),
    "basic": ("authorization", "Basic cmVhZGVyOlNFTlRJTkVMLVBBU1NXT1JE"),
    "headers": ("x-team", HEADER_VALUE),
}

ECHO: dict[str, Any] = {
    "name": "echo",
    "title": "Echo",
    "description": "Says it back.",
    "inputSchema": {
        "type": "object",
        "properties": {"text": {"type": "string"}},
        "required": ["text"],
    },
    "outputSchema": {"type": "object", "properties": {"text": {"type": "string"}}},
    "annotations": {"readOnlyHint": True},
}
LIST_FILES: dict[str, Any] = {"name": "list_files", "inputSchema": {"type": "object"}}


@dataclass
class Upstream:
    """A fake MCP server and the record of what it was asked.

    ``mode`` is the one thing wrong with it. ``demands`` is a header it will
    not answer without, which is how the credential tests see the credential
    arrive.
    """

    mode: str = "ok"
    demands: tuple[str, str] | None = None
    refuse_with: int = 401
    #: Every request seen: method name (or ``None`` for a non-JSON-RPC body)
    #: and the headers it came with.
    seen: list[tuple[str | None, dict[str, str]]] = field(default_factory=list)
    #: The ``initialize`` params, which say who is connecting.
    initialize_params: dict[str, Any] | None = None

    def app(self) -> Starlette:
        return Starlette(routes=[Route("/mcp", self.answer, methods=["POST", "GET", "DELETE"])])

    def transport(self) -> httpx2.ASGITransport:
        return httpx2.ASGITransport(app=self.app())

    async def answer(self, request: Request) -> Response:
        headers = dict(request.headers)
        body: dict[str, Any] | None = None
        if request.method == "POST":
            try:
                body = json.loads(await request.body())
            except ValueError:
                body = None
        method = body.get("method") if body else None
        self.seen.append((method, headers))

        if self.demands is not None and headers.get(self.demands[0]) != self.demands[1]:
            return JSONResponse({"error": "who are you"}, status_code=self.refuse_with)

        match self.mode:
            case "status":
                return PlainTextResponse("nothing here", status_code=self.refuse_with)
            case "redirect":
                return RedirectResponse("http://files.example/elsewhere", status_code=307)
            case "html":
                return PlainTextResponse("<html>a login page</html>", media_type="text/html")
            case "json-but-not-jsonrpc":
                return JSONResponse({"hello": "world"})
            case "declared-too-large":
                return Response(
                    b"{}", media_type="application/json", headers={"content-length": "99999999"}
                )

        assert body is not None
        if method == "initialize":
            self.initialize_params = body["params"]
            capabilities = {} if self.mode == "no-tools" else {"tools": {}}
            return self.result(
                body,
                {
                    "protocolVersion": body["params"]["protocolVersion"],
                    "capabilities": capabilities,
                    "serverInfo": {"name": "files", "title": "Files", "version": "3.1.4"},
                },
            )
        if method == "notifications/initialized":
            return Response(status_code=202)
        if method == "tools/list":
            return await self.tools(body)
        return self.error(body, -32601, "Method not found")

    async def tools(self, body: dict[str, Any]) -> Response:
        cursor = (body.get("params") or {}).get("cursor")
        match self.mode:
            case "rpc-error":
                return self.error(body, -32603, "the listing broke")
            case "never-answers":
                await asyncio.sleep(30)
            case "streamed-too-large":
                return StreamingResponse(self.endless(), media_type="application/json")
            case "endless-pages":
                return self.result(body, {"tools": [LIST_FILES], "nextCursor": "again"})
            case "pages":
                if cursor is None:
                    return self.result(body, {"tools": [LIST_FILES], "nextCursor": "page-2"})
                assert cursor == "page-2"
                return self.result(body, {"tools": [ECHO]})
        return self.result(body, {"tools": [ECHO, LIST_FILES]})

    @staticmethod
    async def endless() -> AsyncIterator[bytes]:
        for _ in range(10_000):
            yield b" " * 1024

    @staticmethod
    def result(body: dict[str, Any], result: dict[str, Any]) -> JSONResponse:
        return JSONResponse({"jsonrpc": "2.0", "id": body["id"], "result": result})

    @staticmethod
    def error(body: dict[str, Any], code: int, message: str) -> JSONResponse:
        return JSONResponse(
            {"jsonrpc": "2.0", "id": body.get("id"), "error": {"code": code, "message": message}}
        )


async def preview(upstream: Upstream, **kwargs: Any) -> Any:
    return await preview_endpoint(ENDPOINT, transport=upstream.transport(), **kwargs)


async def failure(upstream: Upstream, **kwargs: Any) -> EndpointError:
    with pytest.raises(EndpointError) as caught:
        await preview(upstream, **kwargs)
    return caught.value


# --------------------------------------------------------------------------- #
# The preview
# --------------------------------------------------------------------------- #


async def test_the_preview_says_who_answered_and_what_it_offers() -> None:
    found = await preview(Upstream())

    assert (found.name, found.title, found.version) == ("files", "Files", "3.1.4")
    assert found.display_name == "Files"
    assert found.url == ENDPOINT
    # The negotiated version, spelled the way the row's column will hold it.
    assert found.spec_format == f"mcp-{found.protocol_version}"
    assert found.protocol_version.count("-") == 2

    assert found.tool_count == 2
    echo, list_files = found.tools
    assert (echo.name, echo.title, echo.description) == ("echo", "Echo", "Says it back.")
    assert echo.input_schema == ECHO["inputSchema"]
    assert echo.output_schema == ECHO["outputSchema"]
    assert echo.annotations == {"readOnlyHint": True}
    assert (list_files.title, list_files.description) == (None, None)
    assert (list_files.output_schema, list_files.annotations) == (None, None)


async def test_the_snapshot_holds_the_tools_as_the_upstream_sent_them() -> None:
    found = await preview(Upstream())

    assert found.document["mcp"]["serverInfo"] == {
        "name": "files",
        "title": "Files",
        "version": "3.1.4",
    }
    assert found.document["mcp"]["protocolVersion"] == found.protocol_version
    assert found.document["mcp"]["tools"] == [ECHO, LIST_FILES]
    assert len(found.spec_hash) == 64


async def test_the_same_listing_hashes_the_same_and_a_changed_one_does_not() -> None:
    first = await preview(Upstream())
    again = await preview(Upstream())
    changed = await preview(Upstream(mode="pages"))

    assert first.spec_hash == again.spec_hash
    # Same two tools, listed in the other order: a different document.
    assert changed.spec_hash != first.spec_hash


async def test_every_page_of_the_tool_list_is_read() -> None:
    upstream = Upstream(mode="pages")

    found = await preview(upstream)

    assert [tool.name for tool in found.tools] == ["list_files", "echo"]
    assert [method for method, _ in upstream.seen].count("tools/list") == 2


async def test_a_cursor_that_never_ends_is_given_up_on() -> None:
    upstream = Upstream(mode="endless-pages")

    error = await failure(upstream)

    assert isinstance(error, EndpointProtocolError)
    assert str(MAX_PAGES) in str(error)
    assert [method for method, _ in upstream.seen].count("tools/list") == MAX_PAGES


async def test_the_session_says_who_it_is() -> None:
    upstream = Upstream()

    await preview(upstream, http=HttpSettings(user_agent="gateway-under-test/1"))

    assert upstream.initialize_params is not None
    assert upstream.initialize_params["clientInfo"] == {
        "name": CLIENT_NAME,
        "version": __version__,
    }
    assert {headers["user-agent"] for _, headers in upstream.seen} == {"gateway-under-test/1"}


async def test_the_session_is_closed_afterwards_and_nothing_else_is_asked() -> None:
    upstream = Upstream()

    await preview(upstream)

    # The handshake, its notification, one listing: no GET stream, no extra
    # round trips, and nothing after the answer came back.
    assert [method for method, _ in upstream.seen] == [
        "initialize",
        "notifications/initialized",
        "tools/list",
    ]


# --------------------------------------------------------------------------- #
# The credential
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("credential", "presented"),
    [(BEARER, "bearer"), (API_KEY, "api_key"), (BASIC, "basic"), (HEADERS, "headers")],
    ids=["bearer", "api_key", "basic", "headers"],
)
async def test_each_kind_of_credential_reaches_the_upstream_on_every_request(
    credential: Credential, presented: str
) -> None:
    header, value = PRESENTED[presented]
    upstream = Upstream(demands=(header, value))

    found = await preview(upstream, credential=credential)

    assert found.tool_count == 2
    assert len(upstream.seen) == 3
    assert all(headers[header] == value for _, headers in upstream.seen)


async def test_without_a_credential_the_401_is_reported_as_the_credential_problem() -> None:
    error = await failure(Upstream(demands=PRESENTED["bearer"]))

    assert isinstance(error, EndpointStatusError)
    assert error.status_code == 401
    assert error.needs_credentials is True
    assert "HTTP 401 Unauthorized" in str(error)


async def test_a_403_is_the_credential_problem_too() -> None:
    error = await failure(Upstream(demands=PRESENTED["bearer"], refuse_with=403))

    assert isinstance(error, EndpointStatusError)
    assert (error.status_code, error.needs_credentials) == (403, True)


async def test_the_wrong_credential_is_the_same_news_as_none() -> None:
    error = await failure(Upstream(demands=PRESENTED["bearer"]), credential=API_KEY)

    assert isinstance(error, EndpointStatusError)
    assert error.needs_credentials is True


# --------------------------------------------------------------------------- #
# Reachable and wrong
# --------------------------------------------------------------------------- #


async def test_a_404_is_the_url_problem_and_not_the_credential_one() -> None:
    error = await failure(Upstream(mode="status", refuse_with=404))

    assert isinstance(error, EndpointStatusError)
    assert (error.status_code, error.needs_credentials) == (404, False)
    assert "HTTP 404 Not Found" in str(error)


async def test_a_server_error_keeps_its_status() -> None:
    error = await failure(Upstream(mode="status", refuse_with=503))

    assert isinstance(error, EndpointStatusError)
    assert error.status_code == 503


@pytest.mark.parametrize("mode", ["html", "json-but-not-jsonrpc"])
async def test_something_that_is_not_an_mcp_server_is_said_to_be(mode: str) -> None:
    error = await failure(Upstream(mode=mode))

    assert isinstance(error, EndpointProtocolError)
    assert error.status_code is None
    assert "did not answer as an MCP server" in str(error)
    # The one line, not pydantic's whole account of what a message needs.
    assert len(str(error)) < 200


async def test_a_redirect_is_not_followed_and_the_credential_goes_nowhere() -> None:
    upstream = Upstream(mode="redirect")

    error = await failure(upstream, credential=BEARER)

    assert isinstance(error, EndpointProtocolError)
    assert "redirected (HTTP 307)" in str(error)
    # One request, to the URL the operator gave; nothing was asked of
    # wherever the redirect pointed.
    assert len(upstream.seen) == 1


async def test_a_listing_that_fails_is_reported_in_the_upstream_s_words() -> None:
    error = await failure(Upstream(mode="rpc-error"))

    assert isinstance(error, EndpointProtocolError)
    assert "the listing broke" in str(error)


async def test_an_mcp_server_with_no_tools_is_told_apart_from_a_broken_one() -> None:
    upstream = Upstream(mode="no-tools")

    error = await failure(upstream)

    assert isinstance(error, EndpointNoToolsError)
    assert error.name == "Files"
    assert "offers no tools" in str(error)
    # Known from the handshake: the listing was never asked for.
    assert "tools/list" not in [method for method, _ in upstream.seen]


# --------------------------------------------------------------------------- #
# Unreachable
# --------------------------------------------------------------------------- #


async def test_a_host_that_does_not_resolve_is_unreachable() -> None:
    with pytest.raises(EndpointNetworkError) as caught:
        await preview_endpoint(
            "http://mcp.nonexistent.invalid/mcp", http=HttpSettings(timeout_seconds=5)
        )

    assert caught.value.status_code is None
    assert str(caught.value).startswith("Could not reach http://mcp.nonexistent.invalid/mcp: ")


@pytest.mark.parametrize(
    ("url", "reason"),
    [
        ("file:///srv/mcp", "only http:// and https:// endpoints can be used"),
        ("ftp://files.example/mcp", "only http:// and https:// endpoints can be used"),
        ("http:///mcp", "the URL names no host"),
    ],
)
async def test_a_url_the_gateway_will_not_connect_to_never_reaches_the_transport(
    url: str, reason: str
) -> None:
    upstream = Upstream()

    with pytest.raises(EndpointNetworkError) as caught:
        await preview_endpoint(url, transport=upstream.transport())

    assert caught.value.reason == reason
    assert upstream.seen == []


async def test_the_timeout_bounds_a_server_that_takes_the_request_and_never_answers() -> None:
    error = await failure(Upstream(mode="never-answers"), http=HttpSettings(timeout_seconds=0.3))

    assert isinstance(error, EndpointNetworkError)
    assert "no answer within 0.3 seconds" in str(error)


# --------------------------------------------------------------------------- #
# Too much
# --------------------------------------------------------------------------- #


async def test_a_response_that_says_it_is_too_large_is_not_read() -> None:
    error = await failure(Upstream(mode="declared-too-large"))

    assert isinstance(error, EndpointTooLargeError)
    assert error.declared_bytes == 99_999_999
    assert "http.max_response_bytes" in str(error)


async def test_a_response_that_turns_out_too_large_is_abandoned_where_it_crosses() -> None:
    limit = 4096
    error = await failure(
        Upstream(mode="streamed-too-large"), http=HttpSettings(max_response_bytes=limit)
    )

    assert isinstance(error, EndpointTooLargeError)
    assert (error.limit_bytes, error.declared_bytes) == (limit, None)


# --------------------------------------------------------------------------- #
# The session, for the callers that keep one
# --------------------------------------------------------------------------- #


async def test_open_session_hands_over_an_initialised_session() -> None:
    upstream = Upstream()

    async with open_session(ENDPOINT, transport=upstream.transport()) as link:
        assert (link.name, link.title, link.version) == ("files", "Files", "3.1.4")
        assert link.has_tools is True
        listed = await link.session.list_tools()

    assert [tool.name for tool in listed.tools] == ["echo", "list_files"]


async def test_a_caller_s_own_exception_comes_back_as_it_was_raised() -> None:
    class MineError(Exception):
        pass

    with pytest.raises(MineError) as caught:
        async with open_session(ENDPOINT, transport=Upstream().transport()):
            raise MineError("from inside")

    # Not wrapped in the SDK's task group, and not mistaken for one of ours.
    assert str(caught.value) == "from inside"
    assert not isinstance(caught.value, EndpointError)


# --------------------------------------------------------------------------- #
# Nothing carries a secret
# --------------------------------------------------------------------------- #


async def test_no_message_or_log_line_carries_a_credential(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    said: list[str] = []
    for credential in (BEARER, API_KEY, BASIC, HEADERS):
        for upstream in (
            Upstream(demands=("x-never", "sent")),
            Upstream(mode="html"),
            Upstream(mode="redirect"),
            Upstream(mode="declared-too-large"),
            Upstream(mode="rpc-error"),
        ):
            said.append(str(await failure(upstream, credential=credential)))
        await preview(Upstream(), credential=credential)

    everything = "\n".join(said) + caplog.text
    assert "files.example" in everything
    for secret in SECRETS:
        assert secret not in everything
