"""The two guarantees the scenarios are only worth as much as (task 033).

Both of task 033's standing acceptance criteria are properties of the harness
rather than of any one scenario, and both are the kind that decay quietly. A
suite that started reusing a database would go on passing while proving less; a
suite that reached the real network would pass on this machine and fail in CI,
at a time nobody chose. So they are asserted here, once, rather than trusted.
"""

from __future__ import annotations

import httpx
import pytest
import respx
from harness import Gateway, GatewayFactory, World


async def test_a_gateway_starts_with_nothing_registered(gateway: Gateway) -> None:
    """The clean database every scenario begins from.

    Not an empty one: every gateway carries the built-in server from its first
    start. It is disabled, so it contributes no tools, and it is not a
    registration — which is the whole of the difference (task 102).
    """
    assert await gateway.registered_servers() == []
    assert await gateway.tool_names() == []
    assert (await gateway.builtin())["enabled"] is False


async def test_two_gateways_in_one_test_do_not_share_a_database(
    build_gateway: GatewayFactory, world: World
) -> None:
    """Scenario 4 builds two, and what is in one must not be in the other."""
    world.serves_spec("https://specs.test/petstore-3.1.yaml", "petstore-openapi-3.1.yaml")
    one = await build_gateway()
    two = await build_gateway()

    await one.registered("https://specs.test/petstore-3.1.yaml", tool_prefix="petstore")

    assert len(await one.registered_servers()) == 1
    assert await two.registered_servers() == []
    assert one.settings.server.data_dir != two.settings.server.data_dir


async def test_nothing_in_this_suite_can_reach_the_network(
    respx_mock: respx.MockRouter,
) -> None:
    """An httpx request nobody stubbed raises rather than resolving.

    Which is what "offline and deterministic" rests on: a scenario that grew a
    dependency on some real host fails here and now, rather than on the machine
    that has no route to it.
    """
    async with httpx.AsyncClient() as client:
        with pytest.raises(respx.models.AllMockedAssertionError):
            await client.get("https://not-stubbed.example/anything")


async def test_the_gateway_itself_is_not_stubbed(gateway: Gateway, world: World) -> None:
    """The other half: ASGI does not go through the layer respx patches.

    Without that, mocking the outside would mock the thing under test too, and
    every scenario would be asserting against a stub of the gateway.
    """
    assert (await gateway.http.get("/healthz")).status_code == 200
    assert world.router.calls.call_count == 0
