"""The repository layer: what each function stores, and what it refuses to leak.

Every credential in this file starts with ``SENTINEL-``, so one test can take
everything the read paths produce, serialise it, and prove that none of it
carries a credential value.
"""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from mcp_gateway.config import load_settings
from mcp_gateway.crypto import CredentialCipher, CredentialUnreadable, generate_key
from mcp_gateway.db import repo
from mcp_gateway.db.models import Base, MetricBucket, Operation, Server
from mcp_gateway.db.repo import (
    NewServer,
    OperationInput,
    OperationNotFound,
    OperationPatch,
    ServerNotFound,
    ServerPatch,
)
from mcp_gateway.db.session import Database, open_database

API_TOKEN = "SENTINEL-API-TOKEN"
SPEC_TOKEN = "SENTINEL-SPEC-TOKEN"
SECRETS = (API_TOKEN, SPEC_TOKEN, "SENTINEL-REPLACEMENT")

BEARER: dict[str, Any] = {"type": "bearer", "token": API_TOKEN}
SPEC_BEARER: dict[str, Any] = {"type": "bearer", "token": SPEC_TOKEN}


@pytest.fixture
async def database(tmp_path: Path) -> AsyncIterator[Database]:
    """A real SQLite file with the schema built from the models."""
    db = open_database(load_settings(environ={}, cwd=tmp_path))
    async with db.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    try:
        yield db
    finally:
        await db.dispose()


@pytest.fixture
async def session(database: Database) -> AsyncIterator[Any]:
    async with database.session_factory() as session:
        yield session


@pytest.fixture
def cipher() -> CredentialCipher:
    return CredentialCipher(generate_key())


def a_server(slug: str = "petstore", **overrides: Any) -> NewServer:
    """A registrable server; every required field, nothing more."""
    values: dict[str, Any] = {
        "name": slug.title(),
        "slug": slug,
        "tool_prefix": slug,
        "spec_url": f"https://{slug}.example/openapi.json",
        "spec_format": "openapi-3.1",
        "base_url": f"https://{slug}.example/api",
    }
    values.update(overrides)
    return NewServer(**values)


def an_operation(
    op_key: str = "GET /pets", *, prefix: str = "petstore", schema_hash: str = "hash-1"
) -> OperationInput:
    method, path = op_key.split(" ", 1)
    slug = path.strip("/").replace("/", "_").replace("{", "").replace("}", "") or "root"
    return OperationInput(
        op_key=op_key,
        operation_id=f"{method.lower()}_{slug}",
        method=method,
        path=path,
        summary=f"{method} {path}",
        input_schema={"type": "object", "properties": {}},
        input_schema_hash=schema_hash,
        tool_name=f"{prefix}__{method.lower()}_{slug}",
    )


async def a_registered_server(
    session: Any, cipher: CredentialCipher, slug: str = "petstore", **overrides: Any
) -> Server:
    return await repo.create_server(session, a_server(slug, **overrides), cipher=cipher)


async def with_operations(
    session: Any, server: Server, *op_keys: str, selected: bool = True
) -> None:
    """Import operations and, by default, put them all in the tool list."""
    await repo.upsert_operations(
        session, server.id, [an_operation(key, prefix=server.tool_prefix) for key in op_keys]
    )
    if selected:
        await repo.set_selected(session, server.id, op_keys)


# --------------------------------------------------------------------------- #
# Servers
# --------------------------------------------------------------------------- #


async def test_a_registered_server_keeps_what_it_was_given(
    session: Any, cipher: CredentialCipher
) -> None:
    server = await repo.create_server(session, a_server(), cipher=cipher)

    assert server.id == 1
    assert (server.name, server.slug, server.tool_prefix) == ("Petstore", "petstore", "petstore")
    # Defaults: visible, manual, unauthenticated, never refreshed.
    assert (server.enabled, server.auto_refresh, server.needs_attention) == (True, False, False)
    assert (server.auth_type, server.auth_config_encrypted) == ("none", None)
    assert (server.spec_auth_mode, server.last_refresh_at) == ("none", None)


async def test_a_credential_is_stored_encrypted_and_types_itself(
    session: Any, cipher: CredentialCipher
) -> None:
    server = await repo.create_server(session, a_server(credential=BEARER), cipher=cipher)

    # auth_type is taken from the payload, never accepted alongside it: two
    # fields that must agree are two fields that can disagree.
    assert server.auth_type == "bearer"
    assert server.auth_config_encrypted is not None
    assert API_TOKEN not in server.auth_config_encrypted.decode("ascii")


async def test_the_stored_credential_comes_back_through_the_cipher(
    session: Any, cipher: CredentialCipher
) -> None:
    server = await repo.create_server(session, a_server(credential=BEARER), cipher=cipher)

    credential = repo.credential_for(server, cipher)

    assert credential is not None
    assert json.loads(credential.model_dump_json()) == BEARER


async def test_a_server_without_auth_has_no_credential(
    session: Any, cipher: CredentialCipher
) -> None:
    server = await repo.create_server(session, a_server(), cipher=cipher)

    assert repo.credential_for(server, cipher) is None


async def test_a_credential_that_should_be_there_and_is_not_is_unreadable(
    session: Any, cipher: CredentialCipher
) -> None:
    # A half-saved row: the server says it authenticates, with nothing to
    # authenticate with. The proxy hears about it the same way it hears about a
    # lost key, because the operator's fix is the same either way.
    server = await repo.create_server(session, a_server(credential=BEARER), cipher=cipher)
    server.auth_config_encrypted = None

    with pytest.raises(CredentialUnreadable) as exc:
        repo.credential_for(server, cipher)

    assert exc.value.server_id == server.id


@pytest.mark.parametrize(
    ("mode", "expected"),
    [("none", None), ("same_as_api", BEARER), ("custom", SPEC_BEARER)],
)
async def test_each_spec_auth_mode_resolves_to_its_own_credential(
    session: Any, cipher: CredentialCipher, mode: str, expected: dict[str, Any] | None
) -> None:
    server = await repo.create_server(
        session,
        a_server(
            credential=BEARER,
            spec_auth_mode=mode,
            spec_credential=SPEC_BEARER if mode == "custom" else None,
        ),
        cipher=cipher,
    )

    credential = repo.spec_credential_for(server, cipher)

    assert (None if credential is None else json.loads(credential.model_dump_json())) == expected


async def test_custom_spec_auth_without_a_credential_is_refused(
    session: Any, cipher: CredentialCipher
) -> None:
    # Otherwise the mistake surfaces as a 401 on the next refresh, hours later.
    with pytest.raises(ValueError, match="custom"):
        await repo.create_server(session, a_server(spec_auth_mode="custom"), cipher=cipher)


async def test_leaving_custom_mode_drops_the_credential_it_used(
    session: Any, cipher: CredentialCipher
) -> None:
    server = await repo.create_server(
        session,
        a_server(spec_auth_mode="custom", spec_credential=SPEC_BEARER),
        cipher=cipher,
    )

    await repo.update_server(session, server.id, ServerPatch(spec_auth_mode="none"), cipher=cipher)

    # A secret kept for a mode that no longer uses it is a secret kept for nothing.
    assert server.spec_auth_config_encrypted is None
    assert server.spec_auth_type is None


async def test_an_edit_leaves_untouched_fields_alone(
    session: Any, cipher: CredentialCipher
) -> None:
    server = await repo.create_server(session, a_server(credential=BEARER), cipher=cipher)
    stored = server.auth_config_encrypted

    await repo.update_server(
        session, server.id, ServerPatch(name="Pet Store", enabled=False), cipher=cipher
    )

    assert (server.name, server.enabled) == ("Pet Store", False)
    # The form that renders "set" cannot resubmit the value, so leaving the
    # field out has to mean "keep it".
    assert server.auth_config_encrypted == stored
    assert server.auth_type == "bearer"


async def test_a_credential_can_be_replaced(session: Any, cipher: CredentialCipher) -> None:
    server = await repo.create_server(session, a_server(credential=BEARER), cipher=cipher)

    await repo.update_server(
        session,
        server.id,
        ServerPatch(
            credential={"type": "api_key", "header": "X-Key", "value": "SENTINEL-REPLACEMENT"}
        ),
        cipher=cipher,
    )

    assert server.auth_type == "api_key"
    credential = repo.credential_for(server, cipher)
    assert credential is not None
    assert json.loads(credential.model_dump_json())["value"] == "SENTINEL-REPLACEMENT"


async def test_an_explicit_none_clears_a_credential(session: Any, cipher: CredentialCipher) -> None:
    server = await repo.create_server(session, a_server(credential=BEARER), cipher=cipher)

    await repo.update_server(session, server.id, ServerPatch(credential=None), cipher=cipher)

    assert (server.auth_type, server.auth_config_encrypted) == ("none", None)


async def test_editing_a_server_that_is_not_there_says_so(
    session: Any, cipher: CredentialCipher
) -> None:
    with pytest.raises(ServerNotFound) as exc:
        await repo.update_server(session, 404, ServerPatch(name="ghost"), cipher=cipher)

    assert exc.value.server_id == 404


async def test_a_server_can_be_switched_off_without_the_key(
    session: Any, cipher: CredentialCipher
) -> None:
    # The list page's toggle has no business holding a cipher.
    server = await a_registered_server(session, cipher)

    await repo.set_server_enabled(session, server.id, enabled=False)

    assert server.enabled is False


async def test_deleting_a_server_takes_its_operations_and_leaves_its_metrics(
    session: Any, cipher: CredentialCipher
) -> None:
    server = await a_registered_server(session, cipher)
    await with_operations(session, server, "GET /pets")
    session.add(
        MetricBucket(
            bucket_start=dt.datetime(2026, 9, 6, tzinfo=dt.UTC),
            server_id=server.id,
            kind="tool_call",
            calls=3,
        )
    )
    await session.flush()

    await repo.delete_server(session, server.id)

    assert await session.get(Server, server.id) is None
    assert await repo.list_operations(session, server.id) == []
    assert (await session.get(MetricBucket, 1)).calls == 3
    with pytest.raises(ServerNotFound):
        await repo.delete_server(session, server.id)


async def test_a_refresh_that_failed_keeps_the_document_it_had(
    session: Any, cipher: CredentialCipher
) -> None:
    server = await a_registered_server(session, cipher)
    await repo.record_refresh(
        session, server.id, status="ok", spec_hash="abc", spec_snapshot={"a": 1}
    )

    await repo.record_refresh(session, server.id, status="error", error="502 Bad Gateway")

    assert (server.last_refresh_status, server.last_refresh_error) == ("error", "502 Bad Gateway")
    # Still there to diff the next successful refresh against.
    assert (server.spec_hash, server.spec_snapshot) == ("abc", {"a": 1})
    assert server.last_refresh_at is not None


async def test_a_slug_can_be_looked_up_before_it_is_taken(
    session: Any, cipher: CredentialCipher
) -> None:
    await a_registered_server(session, cipher, "petstore")

    assert (await repo.get_server_by_slug(session, "petstore")) is not None
    assert (await repo.get_server_by_slug(session, "store")) is None


# --------------------------------------------------------------------------- #
# Listing and counting
# --------------------------------------------------------------------------- #


async def test_the_list_carries_the_counts_the_table_shows(
    session: Any, cipher: CredentialCipher
) -> None:
    server = await a_registered_server(session, cipher)
    await with_operations(session, server, "GET /pets", "POST /pets", selected=False)
    await repo.set_selected(session, server.id, ["GET /pets"])
    # One that used to exist upstream and no longer does.
    await repo.upsert_operations(session, server.id, [an_operation("GET /pets")])
    await a_registered_server(session, cipher, "another")

    listed = await repo.list_servers(session)

    assert [row.slug for row in listed] == ["another", "petstore"]
    counts = listed[1].counts
    assert (counts.total, counts.selected, counts.new, counts.removed) == (2, 1, 1, 1)


async def test_a_removed_operation_is_not_counted_as_a_live_tool(
    session: Any, cipher: CredentialCipher
) -> None:
    # It is still selected, and it will come back if the endpoint does; it just
    # is not something the gateway is currently exposing.
    server = await a_registered_server(session, cipher)
    await with_operations(session, server, "GET /pets")
    await repo.upsert_operations(session, server.id, [])

    assert (await repo.list_servers(session))[0].counts == repo.OperationCounts(
        total=1, selected=0, removed=1
    )


async def test_a_server_with_no_operations_counts_zero(
    session: Any, cipher: CredentialCipher
) -> None:
    await a_registered_server(session, cipher)

    assert (await repo.list_servers(session))[0].counts.total == 0


async def test_the_detail_view_carries_the_operations(
    session: Any, cipher: CredentialCipher
) -> None:
    server = await a_registered_server(session, cipher, credential=BEARER)
    await with_operations(session, server, "POST /pets", "GET /pets")

    detail = await repo.server_detail(session, server.id)

    assert detail.id == server.id
    # Sorted for reading, not by insertion.
    assert [operation.op_key for operation in detail.operations] == ["GET /pets", "POST /pets"]
    assert detail.counts.total == 2
    assert (detail.auth_type, detail.auth) == ("bearer", "stored")


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({}, ("none", "none")),
        ({"credential": BEARER}, ("stored", "none")),
        ({"credential": BEARER, "spec_auth_mode": "same_as_api"}, ("stored", "stored")),
        # Says "reuse the API credential" with no API credential to reuse.
        ({"spec_auth_mode": "same_as_api"}, ("none", "missing")),
        (
            {"spec_auth_mode": "custom", "spec_credential": SPEC_BEARER},
            ("none", "stored"),
        ),
    ],
)
async def test_the_summary_reports_credential_state_and_nothing_more(
    session: Any,
    cipher: CredentialCipher,
    overrides: dict[str, Any],
    expected: tuple[str, str],
) -> None:
    server = await a_registered_server(session, cipher, **overrides)

    summary = repo.to_summary(server)

    assert (summary.auth, summary.spec_auth) == expected


async def test_a_credential_that_went_missing_shows_as_missing(
    session: Any, cipher: CredentialCipher
) -> None:
    server = await a_registered_server(session, cipher, credential=BEARER)
    server.auth_config_encrypted = None

    assert repo.to_summary(server).auth == "missing"


# --------------------------------------------------------------------------- #
# Operations
# --------------------------------------------------------------------------- #


async def test_an_import_arrives_new_and_unselected(session: Any, cipher: CredentialCipher) -> None:
    server = await a_registered_server(session, cipher)

    sync = await repo.upsert_operations(
        session, server.id, [an_operation("GET /pets"), an_operation("POST /pets")]
    )

    assert sync.inserted == ("GET /pets", "POST /pets")
    assert sync.needs_attention is True
    operations = await repo.list_operations(session, server.id)
    # Nothing reaches MCP that the operator has not ticked (spec §5.4).
    assert [(op.status, op.selected) for op in operations] == [("new", False), ("new", False)]


async def test_an_unchanged_spec_changes_nothing(session: Any, cipher: CredentialCipher) -> None:
    server = await a_registered_server(session, cipher)
    await with_operations(session, server, "GET /pets")

    sync = await repo.upsert_operations(session, server.id, [an_operation("GET /pets")])

    assert sync == repo.OperationSync(unchanged=("GET /pets",))
    assert sync.needs_attention is False


async def test_a_changed_schema_keeps_the_selection_and_the_overrides(
    session: Any, cipher: CredentialCipher
) -> None:
    server = await a_registered_server(session, cipher)
    await with_operations(session, server, "GET /pets")
    operation = (await repo.list_operations(session, server.id))[0]
    await repo.update_operation(
        session,
        operation.id,
        OperationPatch(
            tool_name_override="list_pets",
            description_override="The pets.",
            effective_tool_name="list_pets",
        ),
    )

    sync = await repo.upsert_operations(
        session, server.id, [an_operation("GET /pets", schema_hash="hash-2")]
    )

    assert sync.changed == ("GET /pets",)
    stored = (await repo.list_operations(session, server.id))[0]
    assert stored.status == "changed"
    # A tool a client is already calling does not get renamed behind its back.
    assert (stored.selected, stored.effective_tool_name) == (True, "list_pets")
    assert stored.description_override == "The pets."
    assert stored.input_schema_hash == "hash-2"


async def test_an_operation_that_vanished_is_marked_not_deleted(
    session: Any, cipher: CredentialCipher
) -> None:
    server = await a_registered_server(session, cipher)
    await with_operations(session, server, "GET /pets", "POST /pets")

    sync = await repo.upsert_operations(session, server.id, [an_operation("GET /pets")])

    assert sync.removed == ("POST /pets",)
    gone = (await repo.list_operations(session, server.id, status="removed"))[0]
    # Retained so a rename or a selection survives an endpoint that briefly drops.
    assert (gone.op_key, gone.selected) == ("POST /pets", True)


async def test_an_operation_that_stays_gone_is_not_reported_twice(
    session: Any, cipher: CredentialCipher
) -> None:
    server = await a_registered_server(session, cipher)
    await with_operations(session, server, "GET /pets", "POST /pets")
    await repo.upsert_operations(session, server.id, [an_operation("GET /pets")])

    sync = await repo.upsert_operations(session, server.id, [an_operation("GET /pets")])

    # Nothing new happened, so nothing asks for the operator's attention again.
    assert sync.removed == ()
    assert sync.needs_attention is False


async def test_an_endpoint_that_comes_back_is_restored(
    session: Any, cipher: CredentialCipher
) -> None:
    server = await a_registered_server(session, cipher)
    await with_operations(session, server, "GET /pets")
    await repo.upsert_operations(session, server.id, [])

    sync = await repo.upsert_operations(session, server.id, [an_operation("GET /pets")])

    assert sync.restored == ("GET /pets",)
    assert sync.needs_attention is False
    restored = (await repo.list_operations(session, server.id))[0]
    assert (restored.status, restored.selected) == ("active", True)


async def test_an_unreviewed_operation_stays_unreviewed(
    session: Any, cipher: CredentialCipher
) -> None:
    # Spec §5.4 reads "otherwise → active", but an hourly scheduled refresh must
    # not quietly erase the list of what the operator has yet to look at.
    server = await a_registered_server(session, cipher)
    await repo.upsert_operations(session, server.id, [an_operation("GET /pets")])

    await repo.upsert_operations(session, server.id, [an_operation("GET /pets")])

    assert (await repo.list_operations(session, server.id))[0].status == "new"


async def test_the_spec_s_own_text_is_refreshed(session: Any, cipher: CredentialCipher) -> None:
    server = await a_registered_server(session, cipher)
    await with_operations(session, server, "GET /pets")
    renamed = an_operation("GET /pets").model_copy(update={"summary": "All the pets"})

    await repo.upsert_operations(session, server.id, [renamed])

    assert (await repo.list_operations(session, server.id))[0].summary == "All the pets"


async def test_importing_for_a_server_that_is_not_there_says_so(session: Any) -> None:
    with pytest.raises(ServerNotFound):
        await repo.upsert_operations(session, 404, [an_operation()])


async def test_acknowledging_settles_the_rows_the_operator_reviewed(
    session: Any, cipher: CredentialCipher
) -> None:
    server = await a_registered_server(session, cipher)
    await with_operations(session, server, "GET /pets", "POST /pets")
    await repo.upsert_operations(
        session, server.id, [an_operation("GET /pets", schema_hash="hash-2")]
    )
    await repo.mark_needs_attention(session, server.id)

    await repo.acknowledge_server(session, server.id)

    assert server.needs_attention is False
    statuses = {op.op_key: op.status for op in await repo.list_operations(session, server.id)}
    # Reviewed rows settle; deleting a removed one stays a separate decision.
    assert statuses == {"GET /pets": "active", "POST /pets": "removed"}


async def test_selecting_reports_only_what_it_changed(
    session: Any, cipher: CredentialCipher
) -> None:
    server = await a_registered_server(session, cipher)
    await with_operations(session, server, "GET /pets", "POST /pets", selected=False)

    assert await repo.set_selected(session, server.id, ["GET /pets", "POST /pets"]) == 2
    assert await repo.set_selected(session, server.id, ["GET /pets"]) == 0
    assert await repo.set_selected(session, server.id, ["GET /pets"], selected=False) == 1
    assert await repo.set_selected(session, server.id, []) == 0


async def test_an_operation_of_another_server_is_not_selected_by_key(
    session: Any, cipher: CredentialCipher
) -> None:
    first = await a_registered_server(session, cipher, "petstore")
    second = await a_registered_server(session, cipher, "store")
    await with_operations(session, first, "GET /pets", selected=False)
    await with_operations(session, second, "GET /pets", selected=False)

    await repo.set_selected(session, first.id, ["GET /pets"])

    assert (await repo.list_operations(session, second.id))[0].selected is False


async def test_an_operation_can_be_edited_and_deleted(
    session: Any, cipher: CredentialCipher
) -> None:
    server = await a_registered_server(session, cipher)
    await with_operations(session, server, "GET /pets")
    operation = (await repo.list_operations(session, server.id))[0]

    await repo.update_operation(session, operation.id, OperationPatch(selected=False))
    assert (await repo.list_operations(session, server.id))[0].selected is False

    await repo.delete_operation(session, operation.id)
    assert await repo.list_operations(session, server.id) == []

    with pytest.raises(OperationNotFound):
        await repo.delete_operation(session, operation.id)
    with pytest.raises(OperationNotFound):
        await repo.update_operation(session, operation.id, OperationPatch(selected=True))


async def test_clearing_an_override_is_not_the_same_as_leaving_it_out(
    session: Any, cipher: CredentialCipher
) -> None:
    server = await a_registered_server(session, cipher)
    await with_operations(session, server, "GET /pets")
    operation = (await repo.list_operations(session, server.id))[0]
    await repo.update_operation(session, operation.id, OperationPatch(description_override="Mine."))

    await repo.update_operation(session, operation.id, OperationPatch(selected=False))
    assert (await repo.list_operations(session, server.id))[0].description_override == "Mine."

    await repo.update_operation(session, operation.id, OperationPatch(description_override=None))
    assert (await repo.list_operations(session, server.id))[0].description_override is None


# --------------------------------------------------------------------------- #
# The live tool list
# --------------------------------------------------------------------------- #


async def test_the_tool_list_is_what_the_operator_selected(
    session: Any, cipher: CredentialCipher
) -> None:
    server = await a_registered_server(session, cipher)
    await with_operations(session, server, "GET /pets", "POST /pets", selected=False)
    await repo.set_selected(session, server.id, ["GET /pets"])

    tools = await repo.list_tools(session)

    assert [tool.tool_name for tool in tools] == ["petstore__get_pets"]
    assert (tools[0].server_name, tools[0].base_url) == ("Petstore", server.base_url)
    assert tools[0].input_schema == {"type": "object", "properties": {}}


async def test_a_disabled_server_contributes_no_tools(
    session: Any, cipher: CredentialCipher
) -> None:
    enabled = await a_registered_server(session, cipher, "petstore")
    disabled = await a_registered_server(session, cipher, "store")
    await with_operations(session, enabled, "GET /pets")
    await with_operations(session, disabled, "GET /items")

    await repo.set_server_enabled(session, disabled.id, enabled=False)

    assert [tool.tool_name for tool in await repo.list_tools(session)] == ["petstore__get_pets"]
    assert await repo.get_tool(session, "store__get_items") is None


async def test_a_removed_operation_leaves_the_tool_list(
    session: Any, cipher: CredentialCipher
) -> None:
    server = await a_registered_server(session, cipher)
    await with_operations(session, server, "GET /pets", "POST /pets")

    await repo.upsert_operations(session, server.id, [an_operation("GET /pets")])

    assert [tool.tool_name for tool in await repo.list_tools(session)] == ["petstore__get_pets"]
    assert await repo.get_tool(session, "petstore__post_pets") is None


async def test_tools_are_listed_across_servers_by_name(
    session: Any, cipher: CredentialCipher
) -> None:
    first = await a_registered_server(session, cipher, "petstore")
    second = await a_registered_server(session, cipher, "store")
    await with_operations(session, first, "GET /pets")
    await with_operations(session, second, "GET /items")

    assert [tool.tool_name for tool in await repo.list_tools(session)] == [
        "petstore__get_pets",
        "store__get_items",
    ]


async def test_one_tool_can_be_looked_up_by_the_name_it_was_called_with(
    session: Any, cipher: CredentialCipher
) -> None:
    server = await a_registered_server(session, cipher)
    await with_operations(session, server, "GET /pets/{id}")

    tool = await repo.get_tool(session, "petstore__get_pets_id")

    assert tool is not None
    assert (tool.method, tool.path, tool.server_id) == ("GET", "/pets/{id}", server.id)
    assert await repo.get_tool(session, "petstore__nothing") is None


# --------------------------------------------------------------------------- #
# Runtime settings
# --------------------------------------------------------------------------- #


async def test_a_setting_is_written_read_and_dropped(session: Any) -> None:
    assert await repo.get_setting(session, "refresh_minutes") is None
    assert await repo.get_setting(session, "refresh_minutes", "60") == "60"

    await repo.set_setting(session, "refresh_minutes", "30")
    assert await repo.get_setting(session, "refresh_minutes") == "30"

    await repo.set_setting(session, "refresh_minutes", "15")
    assert await repo.get_setting(session, "refresh_minutes") == "15"

    await repo.set_setting(session, "theme", "dark")
    assert await repo.all_settings(session) == {"refresh_minutes": "15", "theme": "dark"}

    assert await repo.delete_setting(session, "theme") is True
    assert await repo.delete_setting(session, "theme") is False
    assert await repo.all_settings(session) == {"refresh_minutes": "15"}


# --------------------------------------------------------------------------- #
# The promise the whole layer exists to keep
# --------------------------------------------------------------------------- #


async def test_no_read_path_serialises_a_credential(session: Any, cipher: CredentialCipher) -> None:
    # Everything a handler could render or return, for a server with a credential
    # on both the API and the spec URL.
    server = await repo.create_server(
        session,
        a_server(credential=BEARER, spec_auth_mode="custom", spec_credential=SPEC_BEARER),
        cipher=cipher,
    )
    await with_operations(session, server, "GET /pets", "POST /pets")
    await repo.upsert_operations(session, server.id, [an_operation("GET /pets")])

    tool = await repo.get_tool(session, "petstore__get_pets")
    assert tool is not None
    serialised = "\n".join(
        [
            *(row.model_dump_json() for row in await repo.list_servers(session)),
            (await repo.server_detail(session, server.id)).model_dump_json(),
            *(row.model_dump_json() for row in await repo.list_operations(session, server.id)),
            *(row.model_dump_json() for row in await repo.list_tools(session)),
            tool.model_dump_json(),
            json.dumps(await repo.all_settings(session)),
        ]
    )

    for secret in SECRETS:
        assert secret not in serialised
    # Not even the ciphertext leaves the layer: nothing upstream has a use for it.
    assert "auth_config" not in serialised
    assert "encrypted" not in serialised
    # And the state the UI does need is there.
    assert '"auth":"stored"' in serialised and '"spec_auth":"stored"' in serialised


async def test_the_ciphertext_is_reachable_only_through_the_orm_row(
    session: Any, cipher: CredentialCipher
) -> None:
    # The columns are still there for the machinery that needs them; what the
    # read models do is keep them from travelling any further by accident.
    server = await repo.create_server(session, a_server(credential=BEARER), cipher=cipher)

    assert isinstance((await repo.require_server(session, server.id)).auth_config_encrypted, bytes)
    assert not hasattr(repo.to_summary(server), "auth_config_encrypted")


async def test_the_operations_of_one_server_stay_with_it(
    session: Any, cipher: CredentialCipher
) -> None:
    first = await a_registered_server(session, cipher, "petstore")
    second = await a_registered_server(session, cipher, "store")
    await with_operations(session, first, "GET /pets")
    await with_operations(session, second, "GET /items", "POST /items")

    assert len(await repo.list_operations(session, first.id)) == 1
    assert len(await repo.list_operations(session, second.id)) == 2
    assert (await session.get(Operation, 1)).server_id == first.id
