"""Fetching a spec document: auth, redirects, size, and every way to fail.

Every credential in this file starts with ``SENTINEL-``, so one test can collect
what the module produces when things go wrong — messages and log lines — and
prove none of it carries a token.
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
import respx

from mcp_gateway.config import HttpSettings
from mcp_gateway.crypto import (
    ApiKeyCredential,
    BasicCredential,
    BearerCredential,
    Credential,
    CredentialCipher,
    generate_key,
)
from mcp_gateway.db import repo
from mcp_gateway.db.models import Server
from mcp_gateway.openapi.fetch import (
    MAX_REDIRECTS,
    SpecFetchError,
    SpecNetworkError,
    SpecParseError,
    SpecRedirectError,
    SpecStatusError,
    SpecTooLargeError,
    fetch_spec,
)

SPEC_URL = "https://api.example.com/openapi.json"
ELSEWHERE = "https://cdn.example.net/openapi.json"

API_TOKEN = "SENTINEL-API-TOKEN"
SPEC_KEY = "SENTINEL-SPEC-KEY"
SECRETS = (API_TOKEN, SPEC_KEY, "SENTINEL-PASSWORD")

BEARER = BearerCredential(token=API_TOKEN)  # type: ignore[arg-type]
SPEC_API_KEY = ApiKeyCredential(header="X-Api-Key", value=SPEC_KEY)  # type: ignore[arg-type]

DOCUMENT: dict[str, Any] = {
    "openapi": "3.0.3",
    "info": {"title": "Petstore", "version": "1.0.0"},
    "paths": {"/pets": {"get": {"operationId": "listPets", "responses": {}}}},
}

YAML_DOCUMENT = """
openapi: 3.0.3
info:
  title: Petstore
  version: '1.0.0'
paths:
  /pets:
    get:
      operationId: listPets
      responses: {}
"""

#: Headers httpx puts on every request. What is left is what we chose to send.
BOILERPLATE = frozenset({"host", "accept", "accept-encoding", "connection", "user-agent"})


@pytest.fixture
def cipher() -> CredentialCipher:
    return CredentialCipher(generate_key())


def chosen_headers(request: httpx.Request) -> dict[str, str]:
    """The headers this module decided to send, minus httpx's own."""
    return {
        name: value for name, value in request.headers.items() if name.lower() not in BOILERPLATE
    }


def a_server(
    cipher: CredentialCipher,
    *,
    spec_auth_mode: str,
    api: Credential | None = None,
    spec: Credential | None = None,
) -> Server:
    """A server row as the repository would have written it, without a database."""
    return Server(
        kind="openapi",
        id=1,
        name="Petstore",
        tool_prefix="petstore",
        spec_url=SPEC_URL,
        spec_format="openapi-3.0",
        base_url="https://api.example.com",
        auth_type="none" if api is None else api.type,
        auth_config_encrypted=None if api is None else cipher.encrypt_json(api),
        spec_auth_mode=spec_auth_mode,
        spec_auth_type=None if spec is None else spec.type,
        spec_auth_config_encrypted=None if spec is None else cipher.encrypt_json(spec),
    )


class CountingBody:
    """A body that records how much of itself was actually pulled over the wire."""

    def __init__(self, *, chunk_size: int, chunks: int) -> None:
        self._chunk = b"x" * chunk_size
        self._chunks = chunks
        self.total = chunk_size * chunks
        self.sent = 0

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for _ in range(self._chunks):
            self.sent += len(self._chunk)
            yield self._chunk


# --------------------------------------------------------------------------- #
# Spec credentials
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("spec_auth_mode", "expected"),
    [
        ("none", {}),
        ("same_as_api", {"authorization": f"Bearer {API_TOKEN}"}),
        ("custom", {"x-api-key": SPEC_KEY}),
    ],
)
async def test_each_spec_auth_mode_sends_exactly_its_own_headers(
    respx_mock: respx.MockRouter,
    cipher: CredentialCipher,
    spec_auth_mode: str,
    expected: dict[str, str],
) -> None:
    # The modes resolve in the repository (spec §5.1); the fetcher only ever
    # sees "a credential, or none". This is the two halves joined up.
    server = a_server(cipher, spec_auth_mode=spec_auth_mode, api=BEARER, spec=SPEC_API_KEY)
    route = respx_mock.get(SPEC_URL).mock(return_value=httpx.Response(200, json=DOCUMENT))

    await fetch_spec(SPEC_URL, credential=repo.spec_credential_for(server, cipher))

    sent = chosen_headers(route.calls.last.request)
    assert {name.lower(): value for name, value in sent.items()} == expected


async def test_an_anonymous_fetch_sends_nothing_that_looks_like_a_credential(
    respx_mock: respx.MockRouter,
) -> None:
    route = respx_mock.get(SPEC_URL).mock(return_value=httpx.Response(200, json=DOCUMENT))

    await fetch_spec(SPEC_URL)

    request = route.calls.last.request
    assert "authorization" not in request.headers
    assert chosen_headers(request) == {}


async def test_basic_credentials_reach_the_wire_as_basic_auth(
    respx_mock: respx.MockRouter,
) -> None:
    route = respx_mock.get(SPEC_URL).mock(return_value=httpx.Response(200, json=DOCUMENT))

    await fetch_spec(
        SPEC_URL,
        credential=BasicCredential(username="gateway", password="SENTINEL-PASSWORD"),  # type: ignore[arg-type]
    )

    assert route.calls.last.request.headers["authorization"].startswith("Basic ")


# --------------------------------------------------------------------------- #
# Redirects
# --------------------------------------------------------------------------- #


async def test_a_same_origin_redirect_keeps_the_credential(
    respx_mock: respx.MockRouter,
) -> None:
    respx_mock.get(SPEC_URL).mock(
        return_value=httpx.Response(302, headers={"location": "/v2/openapi.json"})
    )
    final = respx_mock.get("https://api.example.com/v2/openapi.json").mock(
        return_value=httpx.Response(200, json=DOCUMENT)
    )

    fetched = await fetch_spec(SPEC_URL, credential=BEARER)

    assert final.calls.last.request.headers["authorization"] == f"Bearer {API_TOKEN}"
    # And the caller learns where the document really came from.
    assert fetched.url == "https://api.example.com/v2/openapi.json"
    assert fetched.requested_url == SPEC_URL
    assert fetched.redirected is True


async def test_a_cross_origin_redirect_drops_the_credential(
    respx_mock: respx.MockRouter,
) -> None:
    # The whole point: a redirect must not be able to hand the token to whoever
    # it names.
    first = respx_mock.get(SPEC_URL).mock(
        return_value=httpx.Response(302, headers={"location": ELSEWHERE})
    )
    second = respx_mock.get(ELSEWHERE).mock(return_value=httpx.Response(200, json=DOCUMENT))

    await fetch_spec(SPEC_URL, credential=BEARER)

    assert first.calls.last.request.headers["authorization"] == f"Bearer {API_TOKEN}"
    assert "authorization" not in second.calls.last.request.headers
    assert API_TOKEN not in str(second.calls.last.request.headers)


async def test_an_api_key_credential_is_dropped_too(respx_mock: respx.MockRouter) -> None:
    # httpx's own redirect handling strips Authorization and nothing else, so a
    # credential in a header of the upstream's choosing is the case that matters.
    respx_mock.get(SPEC_URL).mock(return_value=httpx.Response(302, headers={"location": ELSEWHERE}))
    second = respx_mock.get(ELSEWHERE).mock(return_value=httpx.Response(200, json=DOCUMENT))

    await fetch_spec(SPEC_URL, credential=SPEC_API_KEY)

    assert chosen_headers(second.calls.last.request) == {}


async def test_a_scheme_change_counts_as_leaving_the_origin(
    respx_mock: respx.MockRouter,
) -> None:
    # httpx keeps Authorization across a plain http -> https upgrade. We do not:
    # the token was already sent in the clear once, which is bad enough.
    respx_mock.get("http://api.example.com/openapi.json").mock(
        return_value=httpx.Response(301, headers={"location": SPEC_URL})
    )
    second = respx_mock.get(SPEC_URL).mock(return_value=httpx.Response(200, json=DOCUMENT))

    await fetch_spec("http://api.example.com/openapi.json", credential=BEARER)

    assert "authorization" not in second.calls.last.request.headers


async def test_a_credential_dropped_once_is_not_picked_back_up(
    respx_mock: respx.MockRouter,
) -> None:
    # Out to a third party and back again. A chain that has been off-origin is
    # not a chain to trust with the token.
    respx_mock.get(SPEC_URL).mock(return_value=httpx.Response(302, headers={"location": ELSEWHERE}))
    respx_mock.get(ELSEWHERE).mock(
        return_value=httpx.Response(302, headers={"location": "https://api.example.com/v3.json"})
    )
    home = respx_mock.get("https://api.example.com/v3.json").mock(
        return_value=httpx.Response(200, json=DOCUMENT)
    )

    await fetch_spec(SPEC_URL, credential=BEARER)

    assert "authorization" not in home.calls.last.request.headers


async def test_the_credential_drop_is_reported(
    respx_mock: respx.MockRouter, caplog: pytest.LogCaptureFixture
) -> None:
    # "Losing credentials mid-redirect surfaces as the resulting 401 rather than
    # a silent failure" (spec §5.1) — and there is a line saying why.
    respx_mock.get(SPEC_URL).mock(return_value=httpx.Response(302, headers={"location": ELSEWHERE}))
    respx_mock.get(ELSEWHERE).mock(return_value=httpx.Response(401))

    with caplog.at_level(logging.WARNING), pytest.raises(SpecStatusError) as exc:
        await fetch_spec(SPEC_URL, credential=BEARER)

    assert exc.value.status_code == 401
    assert "credentials were not sent onward" in caplog.text
    assert "cdn.example.net" in caplog.text


async def test_a_relative_redirect_is_resolved_against_the_current_url(
    respx_mock: respx.MockRouter,
) -> None:
    respx_mock.get("https://api.example.com/specs/openapi.json").mock(
        return_value=httpx.Response(307, headers={"location": "../v2/openapi.yaml"})
    )
    final = respx_mock.get("https://api.example.com/v2/openapi.yaml").mock(
        return_value=httpx.Response(200, text=YAML_DOCUMENT)
    )

    fetched = await fetch_spec("https://api.example.com/specs/openapi.json")

    assert final.called
    assert fetched.document["info"]["title"] == "Petstore"


async def test_a_redirect_chain_that_never_ends_is_refused(
    respx_mock: respx.MockRouter,
) -> None:
    route = respx_mock.get(host="api.example.com").mock(
        return_value=httpx.Response(302, headers={"location": "/again"})
    )

    with pytest.raises(SpecRedirectError) as exc:
        await fetch_spec(SPEC_URL)

    assert exc.value.hops == MAX_REDIRECTS
    # Five hops means six requests: the original, then five more.
    assert route.call_count == MAX_REDIRECTS + 1


async def test_redirects_are_ours_to_follow_even_with_a_permissive_client(
    respx_mock: respx.MockRouter,
) -> None:
    # A caller sharing a client configured to follow redirects would otherwise
    # hand the credential straight to the third party httpx obeys.
    respx_mock.get(SPEC_URL).mock(return_value=httpx.Response(302, headers={"location": ELSEWHERE}))
    second = respx_mock.get(ELSEWHERE).mock(return_value=httpx.Response(200, json=DOCUMENT))

    async with httpx.AsyncClient(follow_redirects=True) as client:
        await fetch_spec(SPEC_URL, credential=SPEC_API_KEY, client=client)

    assert chosen_headers(second.calls.last.request) == {}


# --------------------------------------------------------------------------- #
# Statuses and network failures
# --------------------------------------------------------------------------- #


async def test_a_401_carries_its_status_and_the_upstream_explanation(
    respx_mock: respx.MockRouter,
) -> None:
    respx_mock.get(SPEC_URL).mock(return_value=httpx.Response(401, json={"error": "token expired"}))

    with pytest.raises(SpecStatusError) as exc:
        await fetch_spec(SPEC_URL)

    assert exc.value.status_code == 401
    # The signal the wizard turns into "configure spec credentials" (spec §5.1).
    assert exc.value.needs_credentials is True
    assert "401" in str(exc.value)
    assert "token expired" in str(exc.value)


async def test_a_404_is_a_different_problem_from_a_401(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(SPEC_URL).mock(return_value=httpx.Response(404))

    with pytest.raises(SpecStatusError) as exc:
        await fetch_spec(SPEC_URL)

    assert exc.value.status_code == 404
    assert exc.value.needs_credentials is False


async def test_a_very_long_error_body_is_quoted_only_in_part(
    respx_mock: respx.MockRouter,
) -> None:
    respx_mock.get(SPEC_URL).mock(return_value=httpx.Response(500, text="detail " * 5000))

    with pytest.raises(SpecStatusError) as exc:
        await fetch_spec(SPEC_URL)

    assert len(str(exc.value)) < 1000


async def test_a_connection_failure_is_a_network_error(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(SPEC_URL).mock(side_effect=httpx.ConnectError("nodename nor servname"))

    with pytest.raises(SpecNetworkError) as exc:
        await fetch_spec(SPEC_URL)

    assert exc.value.status_code is None
    assert "nodename" in str(exc.value)


async def test_a_timeout_is_a_network_error(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(SPEC_URL).mock(side_effect=httpx.ReadTimeout("timed out"))

    with pytest.raises(SpecNetworkError):
        await fetch_spec(SPEC_URL, http=HttpSettings(timeout_seconds=0.5))


@pytest.mark.parametrize("url", ["file:///etc/passwd", "ftp://example.com/spec.json", "not a url"])
async def test_only_http_urls_are_fetched_at_all(respx_mock: respx.MockRouter, url: str) -> None:
    # A pasted file:// path must not reach the transport, redirected to or not.
    with pytest.raises(SpecNetworkError):
        await fetch_spec(url)

    assert respx_mock.calls.call_count == 0


async def test_a_redirect_to_an_unsupported_scheme_is_refused(
    respx_mock: respx.MockRouter,
) -> None:
    respx_mock.get(SPEC_URL).mock(
        return_value=httpx.Response(302, headers={"location": "file:///etc/passwd"})
    )

    with pytest.raises(SpecNetworkError):
        await fetch_spec(SPEC_URL)


# --------------------------------------------------------------------------- #
# Size
# --------------------------------------------------------------------------- #


async def test_a_body_that_declares_itself_too_large_is_never_downloaded(
    respx_mock: respx.MockRouter,
) -> None:
    route = respx_mock.get(SPEC_URL).mock(
        return_value=httpx.Response(200, headers={"content-length": "99999"}, content=b"x" * 99999)
    )

    with pytest.raises(SpecTooLargeError) as exc:
        await fetch_spec(SPEC_URL, http=HttpSettings(max_response_bytes=1024))

    assert exc.value.limit_bytes == 1024
    assert exc.value.declared_bytes == 99999
    assert route.called


async def test_a_body_over_the_cap_is_abandoned_part_way(respx_mock: respx.MockRouter) -> None:
    # No content-length, so the only way to know is to watch it arrive — and
    # stop, rather than buffer a stream that was never going to be a spec.
    body = CountingBody(chunk_size=512, chunks=200)
    respx_mock.get(SPEC_URL).mock(return_value=httpx.Response(200, content=body))

    with pytest.raises(SpecTooLargeError) as exc:
        await fetch_spec(SPEC_URL, http=HttpSettings(max_response_bytes=1024))

    assert exc.value.declared_bytes is None
    assert body.sent < body.total
    # One chunk's overshoot is what it takes to know the limit was passed.
    assert body.sent <= 1024 + 512


async def test_a_document_at_the_limit_is_accepted(respx_mock: respx.MockRouter) -> None:
    document = {"openapi": "3.0.3", "info": {"title": "x" * 1100, "version": "1"}, "paths": {}}
    payload = json.dumps(document).encode("utf-8")
    respx_mock.get(SPEC_URL).mock(return_value=httpx.Response(200, content=payload))

    fetched = await fetch_spec(SPEC_URL, http=HttpSettings(max_response_bytes=len(payload)))

    assert fetched.size_bytes == len(payload)


# --------------------------------------------------------------------------- #
# Sniffing the document
# --------------------------------------------------------------------------- #


async def test_json_is_parsed(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(SPEC_URL).mock(return_value=httpx.Response(200, json=DOCUMENT))

    fetched = await fetch_spec(SPEC_URL)

    assert fetched.document == DOCUMENT
    assert fetched.parsed_as == "json"
    assert fetched.redirected is False


async def test_yaml_served_as_plain_text_still_parses(respx_mock: respx.MockRouter) -> None:
    # The case that makes sniffing worth doing: the content type is wrong and the
    # document is fine.
    respx_mock.get(SPEC_URL).mock(
        return_value=httpx.Response(
            200, text=YAML_DOCUMENT, headers={"content-type": "text/plain; charset=utf-8"}
        )
    )

    fetched = await fetch_spec(SPEC_URL)

    assert fetched.parsed_as == "yaml"
    assert fetched.document["paths"]["/pets"]["get"]["operationId"] == "listPets"
    assert fetched.content_type == "text/plain; charset=utf-8"


async def test_json_served_as_octet_stream_still_parses(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(SPEC_URL).mock(
        return_value=httpx.Response(
            200,
            content=json.dumps(DOCUMENT).encode("utf-8"),
            headers={"content-type": "application/octet-stream"},
        )
    )

    assert (await fetch_spec(SPEC_URL)).parsed_as == "json"


async def test_a_byte_order_mark_does_not_stop_the_parse(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(SPEC_URL).mock(
        return_value=httpx.Response(200, content=b"\xef\xbb\xbf" + json.dumps(DOCUMENT).encode())
    )

    assert (await fetch_spec(SPEC_URL)).document == DOCUMENT


async def test_a_declared_charset_is_honoured(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(SPEC_URL).mock(
        return_value=httpx.Response(
            200,
            content=json.dumps(DOCUMENT).encode("utf-16"),
            headers={"content-type": "application/json; charset=utf-16"},
        )
    )

    assert (await fetch_spec(SPEC_URL)).document == DOCUMENT


async def test_an_html_page_served_with_a_200_is_not_a_spec(
    respx_mock: respx.MockRouter,
) -> None:
    # A login portal, or a proxy's "you are not signed in" page.
    respx_mock.get(SPEC_URL).mock(
        return_value=httpx.Response(200, html="<html><body>Sign in</body></html>")
    )

    with pytest.raises(SpecParseError) as exc:
        await fetch_spec(SPEC_URL)

    assert "not an object" in str(exc.value)


async def test_an_empty_response_is_not_a_spec(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(SPEC_URL).mock(return_value=httpx.Response(200, content=b"   \n"))

    with pytest.raises(SpecParseError) as exc:
        await fetch_spec(SPEC_URL)

    assert "empty" in str(exc.value)


async def test_something_that_is_neither_json_nor_yaml_is_reported_as_such(
    respx_mock: respx.MockRouter,
) -> None:
    respx_mock.get(SPEC_URL).mock(return_value=httpx.Response(200, content=b"key: [unclosed\n"))

    with pytest.raises(SpecParseError) as exc:
        await fetch_spec(SPEC_URL)

    assert "neither JSON nor YAML" in str(exc.value)


async def test_bytes_that_are_not_text_at_all_are_reported(
    respx_mock: respx.MockRouter,
) -> None:
    respx_mock.get(SPEC_URL).mock(return_value=httpx.Response(200, content=b"\xff\xfe\x00binary"))

    with pytest.raises(SpecParseError):
        await fetch_spec(SPEC_URL)


# --------------------------------------------------------------------------- #
# Plumbing
# --------------------------------------------------------------------------- #


async def test_the_configured_timeout_and_user_agent_are_applied(
    respx_mock: respx.MockRouter,
) -> None:
    route = respx_mock.get(SPEC_URL).mock(return_value=httpx.Response(200, json=DOCUMENT))
    http = HttpSettings(timeout_seconds=3.25)

    await fetch_spec(SPEC_URL, http=http)

    request = route.calls.last.request
    assert request.headers["user-agent"] == http.user_agent
    assert request.extensions["timeout"] == {
        "connect": 3.25,
        "read": 3.25,
        "write": 3.25,
        "pool": 3.25,
    }


async def test_a_borrowed_client_does_not_get_to_widen_the_timeout(
    respx_mock: respx.MockRouter,
) -> None:
    route = respx_mock.get(SPEC_URL).mock(return_value=httpx.Response(200, json=DOCUMENT))

    async with httpx.AsyncClient(timeout=httpx.Timeout(600.0)) as client:
        await fetch_spec(SPEC_URL, http=HttpSettings(timeout_seconds=2.0), client=client)

    assert route.calls.last.request.extensions["timeout"]["read"] == 2.0


async def test_nothing_a_failure_produces_carries_a_credential(
    respx_mock: respx.MockRouter, caplog: pytest.LogCaptureFixture
) -> None:
    # Every failing path at once, with the credential attached, plus anything
    # logged while they ran.
    seen: list[str] = []
    respx_mock.get(SPEC_URL).mock(return_value=httpx.Response(302, headers={"location": ELSEWHERE}))
    responses = [
        httpx.Response(401, text="unauthorized"),
        httpx.Response(200, html="<html>nope</html>"),
        httpx.Response(200, headers={"content-length": "99999"}, content=b"x" * 99999),
    ]
    elsewhere = respx_mock.get(ELSEWHERE)

    with caplog.at_level(logging.DEBUG):
        for response in responses:
            elsewhere.mock(return_value=response)
            # Whatever goes wrong, it arrives as the one base class.
            with pytest.raises(SpecFetchError) as exc:
                await fetch_spec(SPEC_URL, credential=BEARER, http=HttpSettings())
            seen.append(str(exc.value))

    haystack = "\n".join([*seen, caplog.text])
    for secret in SECRETS:
        assert secret not in haystack
