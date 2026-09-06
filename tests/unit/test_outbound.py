"""The one place a stored credential becomes request headers.

Both the spec fetcher (task 008) and the tool-call proxy (task 016) send their
credentials through here, so these tests are what stops the two from drifting
apart. Every secret starts with ``SENTINEL-``.
"""

from __future__ import annotations

import base64

import httpx
import pytest
from fastapi import FastAPI

from mcp_gateway.config import HttpSettings
from mcp_gateway.crypto import (
    ApiKeyCredential,
    BasicCredential,
    BearerCredential,
    HeadersCredential,
    parse_credential,
)
from mcp_gateway.outbound import (
    credential_header_names,
    credential_headers,
    origin_of,
    outbound_client,
    outbound_service,
    same_origin,
)


def test_no_credential_produces_no_headers() -> None:
    # Which is why a caller can apply this without asking whether there is one.
    assert credential_headers(None) == {}


def test_a_bearer_token_becomes_an_authorization_header() -> None:
    credential = BearerCredential(token="SENTINEL-TOKEN")

    assert credential_headers(credential) == {"Authorization": "Bearer SENTINEL-TOKEN"}


def test_an_api_key_goes_in_the_header_the_upstream_named() -> None:
    credential = ApiKeyCredential(header="X-Api-Key", value="SENTINEL-APIKEY")

    assert credential_headers(credential) == {"X-Api-Key": "SENTINEL-APIKEY"}


def test_basic_auth_is_base64_of_the_pair() -> None:
    credential = BasicCredential(username="gateway", password="SENTINEL-PASSWORD")

    encoded = base64.b64encode(b"gateway:SENTINEL-PASSWORD").decode("ascii")
    assert credential_headers(credential) == {"Authorization": f"Basic {encoded}"}
    # And the upstream can read it back.
    header = credential_headers(credential)["Authorization"]
    assert base64.b64decode(header.removeprefix("Basic ")) == b"gateway:SENTINEL-PASSWORD"


def test_a_header_map_is_sent_as_written() -> None:
    credential = HeadersCredential(
        headers={"X-One": "SENTINEL-ONE", "X-Two": "SENTINEL-TWO"}  # type: ignore[dict-item]
    )

    assert credential_headers(credential) == {"X-One": "SENTINEL-ONE", "X-Two": "SENTINEL-TWO"}


def test_a_credential_parsed_from_storage_builds_the_same_headers() -> None:
    # The refresh path hands over a model it has just decrypted, the wizard hands
    # over one built from a form. They are the same credential and must send the
    # same thing.
    from_storage = parse_credential({"type": "bearer", "token": "SENTINEL-TOKEN"})

    assert credential_headers(from_storage) == credential_headers(
        BearerCredential(token="SENTINEL-TOKEN")
    )


def test_the_occupied_header_names_are_reported_lowercased() -> None:
    # Spec §5.3 drops header parameters the credential already supplies, so the
    # model cannot overwrite the gateway's own authentication with an argument.
    credential = ApiKeyCredential(header="X-Api-Key", value="SENTINEL-APIKEY")

    assert credential_header_names(credential) == {"x-api-key"}
    assert credential_header_names(None) == frozenset()
    assert credential_header_names(BearerCredential(token="SENTINEL-TOKEN")) == {"authorization"}


@pytest.mark.parametrize(
    ("one", "other", "expected"),
    [
        ("https://api.example.com/a", "https://api.example.com/b", True),
        # The default port is the port.
        ("https://api.example.com/a", "https://api.example.com:443/b", True),
        ("http://api.example.com/a", "http://api.example.com:80/b", True),
        # A host differing only in case is the same host.
        ("https://API.Example.com/a", "https://api.example.com/a", True),
        ("https://api.example.com/a", "https://cdn.example.com/a", False),
        ("https://api.example.com/a", "https://api.example.com:8443/a", False),
        # Same host, different scheme: httpx would keep an Authorization header
        # across this one. We do not.
        ("http://api.example.com/a", "https://api.example.com/a", False),
    ],
)
def test_what_counts_as_the_same_origin(one: str, other: str, expected: bool) -> None:
    assert same_origin(httpx.URL(one), httpx.URL(other)) is expected


def test_an_origin_is_scheme_host_and_port() -> None:
    assert origin_of(httpx.URL("https://api.example.com/openapi.json?token=x")) == (
        "https",
        "api.example.com",
        443,
    )


def test_the_client_carries_the_configured_limits() -> None:
    http = HttpSettings(timeout_seconds=7.5, max_response_bytes=4096)

    client = outbound_client(http)

    assert client.timeout.connect == 7.5
    assert client.headers["user-agent"] == http.user_agent
    # Redirects are followed by the caller, which knows which headers are secret.
    assert client.follow_redirects is False


async def test_the_shared_client_lives_exactly_as_long_as_the_app() -> None:
    """The pool opens on startup and is closed and forgotten on the way down.

    A client left on ``app.state`` after shutdown is a pool bound to an event
    loop that has stopped, which fails at the next use rather than here.
    """
    app = FastAPI()
    service = outbound_service(HttpSettings(timeout_seconds=3.0))

    async with service(app):
        client = app.state.http_client
        assert isinstance(client, httpx.AsyncClient)
        assert client.timeout.connect == 3.0
        assert client.is_closed is False

    assert app.state.http_client is None
    assert client.is_closed is True
