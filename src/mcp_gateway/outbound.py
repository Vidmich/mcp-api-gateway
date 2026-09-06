"""What every outbound HTTP call the gateway makes has in common.

The gateway calls upstream twice over: once to fetch a spec document (spec §5.1)
and once per tool call to reach the API that document describes (spec §6). The
piece those two must never disagree about is how a stored credential becomes
request headers — a credential applied one way here and another way there is a
bug that surfaces only as a 401 from one particular upstream, long after the
change that caused it. So it is written once, here.

Nothing in this module logs or renders a credential. It turns one into headers,
which go straight to httpx and nowhere else.
"""

from __future__ import annotations

import base64
from typing import Final, TypeAlias, assert_never

import httpx

from mcp_gateway.config import HttpSettings
from mcp_gateway.crypto import (
    ApiKeyCredential,
    BasicCredential,
    BearerCredential,
    Credential,
    HeadersCredential,
)

#: Scheme, host, port — what has to stay equal for a credential to keep being
#: sent across a redirect (spec §5.1).
Origin: TypeAlias = tuple[str, str, int]

AUTHORIZATION: Final = "Authorization"

#: Ports that a URL leaves out when they are the scheme's own, so ``https://x``
#: and ``https://x:443`` are recognised as the one origin they are.
DEFAULT_PORTS: Final[dict[str, int]] = {"http": 80, "https": 443}


def credential_headers(credential: Credential | None) -> dict[str, str]:
    """The headers that present ``credential`` to an upstream.

    ``None`` — a server that authenticates with nothing — yields no headers,
    which is why callers can apply this unconditionally.
    """
    if credential is None:
        return {}
    match credential:
        case BearerCredential():
            return {AUTHORIZATION: f"Bearer {credential.token.get_secret_value()}"}
        case ApiKeyCredential():
            return {credential.header: credential.value.get_secret_value()}
        case BasicCredential():
            # RFC 7617. The username is not a secret, but it shares the encoding
            # with the password, so the pair is built in one place.
            pair = f"{credential.username}:{credential.password.get_secret_value()}"
            encoded = base64.b64encode(pair.encode("utf-8")).decode("ascii")
            return {AUTHORIZATION: f"Basic {encoded}"}
        case HeadersCredential():
            return {name: value.get_secret_value() for name, value in credential.headers.items()}
        case _:  # pragma: no cover - exhaustive over the Credential union
            assert_never(credential)


def credential_header_names(credential: Credential | None) -> frozenset[str]:
    """The header names ``credential`` occupies, lowercased.

    Spec §5.3 drops header *parameters* an operation declares when the stored
    credential already supplies that header, so a model cannot overwrite the
    gateway's own authentication by passing an argument. Header names are
    case-insensitive, so the comparison is made on lowercase.
    """
    return frozenset(name.lower() for name in credential_headers(credential))


def origin_of(url: httpx.URL) -> Origin:
    """The origin of ``url``: scheme, host, and port with the default filled in."""
    return (url.scheme, url.host, url.port or DEFAULT_PORTS.get(url.scheme, 0))


def same_origin(one: httpx.URL, other: httpx.URL) -> bool:
    """Whether two URLs are the same origin, and so may share a credential."""
    return origin_of(one) == origin_of(other)


def outbound_client(http: HttpSettings) -> httpx.AsyncClient:
    """A client carrying the configured outbound limits (spec §2).

    ``follow_redirects`` is off. httpx strips only ``Authorization`` when a
    redirect leaves the origin — an ``api_key`` or ``headers`` credential sits in
    a header of the upstream's own choosing, and httpx has no way to know it is a
    secret, so it would forward it to wherever the redirect points. Redirects are
    therefore followed by hand, by the caller that knows what the headers mean.
    """
    return httpx.AsyncClient(
        timeout=httpx.Timeout(http.timeout_seconds),
        headers={"User-Agent": http.user_agent},
        follow_redirects=False,
    )


__all__ = [
    "AUTHORIZATION",
    "Origin",
    "credential_header_names",
    "credential_headers",
    "origin_of",
    "outbound_client",
    "same_origin",
]
