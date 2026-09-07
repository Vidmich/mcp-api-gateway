"""The refresh diff engine: what a second reading of a spec does to a server.

Spec §5.4, task 025.

Every test here registers a real server from a v1 document through the wizard's
own save, serves it a v2 document, and asks what the database looks like
afterwards. That is deliberate: the interesting claims of a refresh are all
claims about state that survived it — a selection, an override, a tool name a
client is already calling — and the only thing that can answer those is a row.

The v1/v2 pair is one fixture with all four transitions in it at once, because
the transitions interact. An operation that changed while another was removed
and a third appeared is the normal case, and a diff that gets each of them right
in isolation can still get the set wrong.
"""

from __future__ import annotations

import copy
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from mcp_gateway import refresh
from mcp_gateway.config import load_settings
from mcp_gateway.crypto import ApiKeyCredential, CredentialCipher, generate_key
from mcp_gateway.db import repo
from mcp_gateway.db.models import Base, Operation, Server
from mcp_gateway.db.session import Database, open_database
from mcp_gateway.openapi.ingest import preview_spec
from mcp_gateway.web.picker import register
from mcp_gateway.web.wizard import PendingServer, WizardForm

SPEC_URL = "https://petstore.example/openapi.json"
OTHER_SPEC_URL = "https://billing.example/openapi.json"

LIST_PETS = "GET /pets"
ADD_PET = "POST /pets"
ONE_PET = "GET /pets/{petId}"
LIST_TOYS = "GET /toys"

#: A spec credential that exists only here. A refresh has to send it, and it
#: must never turn up anywhere else.
SPEC_KEY = "SENTINEL-SPEC-KEY"


def a_document(**paths: Any) -> dict[str, Any]:
    return {
        "openapi": "3.0.3",
        "info": {"title": "Petstore", "version": "1.0.0"},
        "servers": [{"url": "https://api.petstore.example/v2"}],
        "paths": dict(paths),
    }


def an_id_parameter(name: str = "petId") -> dict[str, Any]:
    return {"name": name, "in": "path", "required": True, "schema": {"type": "string"}}


#: What the server was registered from.
V1 = a_document(
    **{
        "/pets": {
            "get": {"operationId": "listPets", "summary": "List pets", "responses": {}},
            "post": {"operationId": "addPet", "summary": "Add a pet", "responses": {}},
        },
        "/pets/{petId}": {
            "get": {
                "operationId": "getPet",
                "summary": "One pet",
                "parameters": [an_id_parameter()],
                "responses": {},
            }
        },
    }
)

#: The same service, later. ``GET /pets`` gained a query parameter, so its input
#: schema moved; ``POST /pets`` is gone; ``GET /toys`` is new; ``GET /pets/{petId}``
#: is untouched. All four transitions of spec §5.4, in one document.
V2 = a_document(
    **{
        "/pets": {
            "get": {
                "operationId": "listPets",
                "summary": "List pets",
                "parameters": [{"name": "limit", "in": "query", "schema": {"type": "integer"}}],
                "responses": {},
            }
        },
        "/pets/{petId}": {
            "get": {
                "operationId": "getPet",
                "summary": "One pet",
                "parameters": [an_id_parameter()],
                "responses": {},
            }
        },
        "/toys": {"get": {"operationId": "listToys", "summary": "List toys", "responses": {}}},
    }
)


# --------------------------------------------------------------------------- #
# The world these tests run in
# --------------------------------------------------------------------------- #


@pytest.fixture
async def database(tmp_path: Path) -> AsyncIterator[Database]:
    db = open_database(load_settings(environ={}, cwd=tmp_path))
    async with db.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    try:
        yield db
    finally:
        await db.dispose()


@pytest.fixture
async def session(database: Database) -> AsyncIterator[AsyncSession]:
    async with database.session_factory() as session:
        yield session


@pytest.fixture
def cipher() -> CredentialCipher:
    return CredentialCipher(generate_key())


class Announcements:
    """A stand-in for the MCP endpoint's ``tools_changed``."""

    def __init__(self) -> None:
        self.count = 0

    async def __call__(self) -> None:
        self.count += 1


def serves(respx_mock: respx.MockRouter, document: Any, url: str = SPEC_URL) -> respx.Route:
    return respx_mock.get(url).mock(return_value=httpx.Response(200, json=document))


async def a_server(
    session: AsyncSession,
    cipher: CredentialCipher,
    *,
    url: str = SPEC_URL,
    name: str = "Petstore",
    prefix: str = "petstore",
    selection: tuple[str, ...] | None = None,
    spec_credential: Any = None,
) -> Server:
    """Register a server the way the wizard does, from a real document."""
    form = WizardForm(
        spec_url=url,
        name=name,
        spec_auth_mode="custom" if spec_credential else "none",
        spec_credential=spec_credential,
    )
    preview = await preview_spec(url, spec_credential=spec_credential)
    pending = PendingServer(form=form, preview=preview)
    keys = tuple(operation.op_key for operation in preview.operations)
    server = await register(
        session,
        pending,
        prefix=prefix,
        selection=keys if selection is None else selection,
        cipher=cipher,
    )
    await session.commit()
    return server


async def rows(session: AsyncSession, server_id: int) -> dict[str, Operation]:
    found = await session.scalars(select(Operation).where(Operation.server_id == server_id))
    return {row.op_key: row for row in found}


async def statuses(session: AsyncSession, server_id: int) -> dict[str, str]:
    return {key: row.status for key, row in (await rows(session, server_id)).items()}


async def tool_names(session: AsyncSession) -> list[str]:
    return [row.tool_name for row in await repo.list_tools(session)]


# --------------------------------------------------------------------------- #
# The four transitions
# --------------------------------------------------------------------------- #


@respx.mock
async def test_a_second_reading_sorts_every_operation_into_its_transition(
    session: AsyncSession, cipher: CredentialCipher
) -> None:
    serves(respx.mock, V1)
    server = await a_server(session, cipher)

    serves(respx.mock, V2)
    report = await refresh.refresh_server(session, server.id, cipher=cipher)

    assert report.outcome == "updated"
    assert await statuses(session, server.id) == {
        # Its input schema moved, because the document grew a query parameter.
        LIST_PETS: "changed",
        # Gone from the document, kept in the database (spec §4).
        ADD_PET: "removed",
        # Present in both and identical: nothing to review.
        ONE_PET: "active",
        # Not in the document the server was registered from.
        LIST_TOYS: "new",
    }


@respx.mock
async def test_the_report_names_what_moved_and_leaves_out_what_did_not(
    session: AsyncSession, cipher: CredentialCipher
) -> None:
    serves(respx.mock, V1)
    server = await a_server(session, cipher)

    serves(respx.mock, V2)
    report = await refresh.refresh_server(session, server.id, cipher=cipher)

    assert {change.op_key: change.status for change in report.changes} == {
        LIST_PETS: "changed",
        ADD_PET: "removed",
        LIST_TOYS: "new",
    }
    assert report.counts == {"new": 1, "changed": 1, "removed": 1, "restored": 0}
    assert ONE_PET not in {change.op_key for change in report.changes}


@respx.mock
async def test_a_refresh_that_found_something_flags_the_server(
    session: AsyncSession, cipher: CredentialCipher
) -> None:
    serves(respx.mock, V1)
    server = await a_server(session, cipher)
    assert server.needs_attention is False

    serves(respx.mock, V2)
    report = await refresh.refresh_server(session, server.id, cipher=cipher)

    assert report.needs_attention is True
    assert (await repo.require_server(session, server.id)).needs_attention is True


@respx.mock
async def test_an_operation_that_came_back_is_reported_but_is_not_news(
    session: AsyncSession, cipher: CredentialCipher
) -> None:
    """A ``removed`` operation the upstream restores is neither new nor changed.

    It keeps everything it had, including its selection, because it is the same
    operation it always was — which is exactly why ``removed`` rows are kept
    rather than deleted.
    """
    serves(respx.mock, V1)
    server = await a_server(session, cipher)

    serves(respx.mock, V2)
    await refresh.refresh_server(session, server.id, cipher=cipher)
    await repo.acknowledge_server(session, server.id)
    await session.commit()

    serves(respx.mock, V1)
    report = await refresh.refresh_server(session, server.id, cipher=cipher)

    assert report.counts["restored"] == 1
    restored = (await rows(session, server.id))[ADD_PET]
    assert (restored.status, restored.selected) == ("active", True)


# --------------------------------------------------------------------------- #
# What a refresh must never do
# --------------------------------------------------------------------------- #


@respx.mock
async def test_a_new_operation_is_never_exposed_by_the_refresh_that_found_it(
    session: AsyncSession, cipher: CredentialCipher
) -> None:
    serves(respx.mock, V1)
    server = await a_server(session, cipher)

    serves(respx.mock, V2)
    report = await refresh.refresh_server(session, server.id, cipher=cipher)

    assert (await rows(session, server.id))[LIST_TOYS].selected is False
    assert [change.selected for change in report.changes if change.status == "new"] == [False]
    assert "petstore__listToys" not in await tool_names(session)


@respx.mock
async def test_a_changed_operation_keeps_its_selection_and_its_overrides(
    session: AsyncSession, cipher: CredentialCipher
) -> None:
    serves(respx.mock, V1)
    server = await a_server(session, cipher)
    stored = (await rows(session, server.id))[LIST_PETS]
    await repo.update_operation(
        session,
        stored.id,
        repo.OperationPatch(
            selected=True,
            tool_name_override="every_pet",
            description_override="The one the prompts use.",
            effective_tool_name="every_pet",
        ),
    )
    await session.commit()

    serves(respx.mock, V2)
    await refresh.refresh_server(session, server.id, cipher=cipher)

    changed = (await rows(session, server.id))[LIST_PETS]
    assert changed.status == "changed"
    assert changed.selected is True
    assert changed.tool_name_override == "every_pet"
    assert changed.description_override == "The one the prompts use."
    assert changed.effective_tool_name == "every_pet"
    # The point of keeping the name: the tool a client is calling is still there.
    assert "every_pet" in await tool_names(session)


@respx.mock
async def test_a_deselected_operation_stays_deselected_when_it_changes(
    session: AsyncSession, cipher: CredentialCipher
) -> None:
    serves(respx.mock, V1)
    server = await a_server(session, cipher, selection=(ONE_PET,))

    serves(respx.mock, V2)
    await refresh.refresh_server(session, server.id, cipher=cipher)

    assert (await rows(session, server.id))[LIST_PETS].selected is False


@respx.mock
async def test_the_schema_of_a_changed_operation_is_the_new_one(
    session: AsyncSession, cipher: CredentialCipher
) -> None:
    """The row's text and schema follow the document; only the edits do not."""
    serves(respx.mock, V1)
    server = await a_server(session, cipher)
    before = (await rows(session, server.id))[LIST_PETS].input_schema_hash

    serves(respx.mock, V2)
    await refresh.refresh_server(session, server.id, cipher=cipher)

    after = (await rows(session, server.id))[LIST_PETS]
    assert after.input_schema_hash != before
    assert "limit" in after.input_schema.get("properties", {})


@respx.mock
async def test_a_removed_operation_stops_being_a_tool_without_being_deleted(
    session: AsyncSession, cipher: CredentialCipher
) -> None:
    serves(respx.mock, V1)
    server = await a_server(session, cipher)
    assert "petstore__addPet" in await tool_names(session)

    serves(respx.mock, V2)
    await refresh.refresh_server(session, server.id, cipher=cipher)

    assert "petstore__addPet" not in await tool_names(session)
    gone = (await rows(session, server.id))[ADD_PET]
    assert (gone.status, gone.selected) == ("removed", True)


@respx.mock
async def test_a_refresh_does_not_clear_needs_attention(
    session: AsyncSession, cipher: CredentialCipher
) -> None:
    """Only acknowledging clears the flag (spec §5.4), never a later refresh."""
    serves(respx.mock, V1)
    server = await a_server(session, cipher)

    serves(respx.mock, V2)
    await refresh.refresh_server(session, server.id, cipher=cipher)
    # A second refresh of the same document: nothing new, and still flagged.
    report = await refresh.refresh_server(session, server.id, cipher=cipher)

    assert report.outcome == "unchanged"
    assert (await repo.require_server(session, server.id)).needs_attention is True


# --------------------------------------------------------------------------- #
# The unchanged document
# --------------------------------------------------------------------------- #


@respx.mock
async def test_an_unchanged_spec_short_circuits_on_the_hash(
    session: AsyncSession, cipher: CredentialCipher
) -> None:
    serves(respx.mock, V1)
    server = await a_server(session, cipher)
    before = await rows(session, server.id)
    seen = {key: row.last_seen_at for key, row in before.items()}

    serves(respx.mock, V1)
    report = await refresh.refresh_server(session, server.id, cipher=cipher)

    assert report.outcome == "unchanged"
    assert report.changes == ()
    assert report.spec_hash == report.previous_hash == server.spec_hash
    after = await rows(session, server.id)
    # Not even ``last_seen_at`` moves: the short circuit is before the upsert.
    assert {key: row.last_seen_at for key, row in after.items()} == seen
    assert {key: row.status for key, row in after.items()} == {
        key: row.status for key, row in before.items()
    }


@respx.mock
async def test_an_unchanged_spec_still_records_that_somebody_looked(
    session: AsyncSession, cipher: CredentialCipher
) -> None:
    serves(respx.mock, V1)
    server = await a_server(session, cipher)
    assert server.last_refresh_at is None

    serves(respx.mock, V1)
    report = await refresh.refresh_server(session, server.id, cipher=cipher)

    stored = await repo.require_server(session, server.id)
    assert stored.last_refresh_status == "ok"
    assert stored.last_refresh_error is None
    assert stored.last_refresh_at == report.at


@respx.mock
async def test_a_document_that_only_reordered_its_keys_is_unchanged(
    session: AsyncSession, cipher: CredentialCipher
) -> None:
    """The hash is of the canonical rendering, so key order is not a change."""
    serves(respx.mock, V1)
    server = await a_server(session, cipher)

    shuffled = copy.deepcopy(V1)
    shuffled["paths"] = dict(reversed(list(shuffled["paths"].items())))
    serves(respx.mock, shuffled)
    report = await refresh.refresh_server(session, server.id, cipher=cipher)

    assert report.outcome == "unchanged"


# --------------------------------------------------------------------------- #
# When it goes wrong
# --------------------------------------------------------------------------- #


@respx.mock
async def test_a_fetch_failure_is_recorded_and_changes_nothing_else(
    session: AsyncSession, cipher: CredentialCipher
) -> None:
    serves(respx.mock, V1)
    server = await a_server(session, cipher)
    before = {
        key: (row.status, row.selected) for key, row in (await rows(session, server.id)).items()
    }
    hash_before = server.spec_hash

    respx.mock.get(SPEC_URL).mock(return_value=httpx.Response(503))
    report = await refresh.refresh_server(session, server.id, cipher=cipher)

    assert report.outcome == "failed"
    assert report.error is not None and "503" in report.error
    assert report.changes == ()
    stored = await repo.require_server(session, server.id)
    assert stored.last_refresh_status == "error"
    assert stored.last_refresh_error == report.error
    # The document that last read is still the one a later refresh diffs against.
    assert stored.spec_hash == hash_before
    assert stored.spec_snapshot is not None
    assert {
        key: (row.status, row.selected) for key, row in (await rows(session, server.id)).items()
    } == before


@respx.mock
async def test_a_document_that_cannot_be_parsed_is_a_failure_not_a_crash(
    session: AsyncSession, cipher: CredentialCipher
) -> None:
    serves(respx.mock, V1)
    server = await a_server(session, cipher)

    respx.mock.get(SPEC_URL).mock(
        return_value=httpx.Response(200, json={"swagger": "1.2", "paths": {}})
    )
    report = await refresh.refresh_server(session, server.id, cipher=cipher)

    assert report.outcome == "failed"
    assert await statuses(session, server.id) == {
        LIST_PETS: "active",
        ADD_PET: "active",
        ONE_PET: "active",
    }


@respx.mock
async def test_a_credential_that_cannot_be_decrypted_is_a_failure(
    session: AsyncSession, cipher: CredentialCipher
) -> None:
    """A key that has moved on is a refresh that fails, with the reason stored."""
    serves(respx.mock, V1)
    server = await a_server(
        session,
        cipher,
        spec_credential=ApiKeyCredential(header="X-Spec-Key", value=SPEC_KEY),
    )

    report = await refresh.refresh_server(
        session, server.id, cipher=CredentialCipher(generate_key())
    )

    assert report.outcome == "failed"
    assert report.error is not None
    stored = await repo.require_server(session, server.id)
    assert stored.last_refresh_status == "error"


@respx.mock
async def test_a_new_operation_whose_name_is_taken_refuses_the_whole_refresh(
    session: AsyncSession, cipher: CredentialCipher
) -> None:
    """Half a document is not a state worth storing (spec §5.3).

    A second server has renamed one of its own tools to the name this document's
    new operation would want. Nothing here can be written, so nothing is — and
    the recorded error names both sides so the operator can settle it.
    """
    serves(respx.mock, V1)
    server = await a_server(session, cipher)
    serves(respx.mock, a_document(**{"/invoices": V1["paths"]["/pets"]}), url=OTHER_SPEC_URL)
    other = await a_server(session, cipher, url=OTHER_SPEC_URL, name="Billing", prefix="billing")
    squatter = (await rows(session, other.id))["GET /invoices"]
    await repo.update_operation(
        session,
        squatter.id,
        repo.OperationPatch(
            tool_name_override="petstore__listToys", effective_tool_name="petstore__listToys"
        ),
    )
    await session.commit()

    serves(respx.mock, V2)
    report = await refresh.refresh_server(session, server.id, cipher=cipher)

    assert report.outcome == "failed"
    assert report.error is not None and "petstore__listToys" in report.error
    assert LIST_TOYS not in await rows(session, server.id)
    assert (await rows(session, server.id))[ADD_PET].status == "active"


@respx.mock
async def test_a_new_operation_may_not_take_a_name_its_own_server_holds(
    session: AsyncSession, cipher: CredentialCipher
) -> None:
    """A sibling holds its name against a newcomer just as a stranger would.

    ``check_conflicts`` excludes the server being planned for, because its other
    caller is a rename that replaces every row. A refresh replaces none of them.
    """
    serves(respx.mock, V1)
    server = await a_server(session, cipher)
    stored = (await rows(session, server.id))[ONE_PET]
    await repo.update_operation(
        session,
        stored.id,
        repo.OperationPatch(
            tool_name_override="petstore__listToys", effective_tool_name="petstore__listToys"
        ),
    )
    await session.commit()

    serves(respx.mock, V2)
    report = await refresh.refresh_server(session, server.id, cipher=cipher)

    assert report.outcome == "failed"
    assert report.error is not None and ONE_PET in report.error
    assert LIST_TOYS not in await rows(session, server.id)


async def test_a_server_that_is_not_there_is_not_a_failed_refresh(
    session: AsyncSession, cipher: CredentialCipher
) -> None:
    with pytest.raises(repo.ServerNotFound):
        await refresh.refresh_server(session, 404, cipher=cipher)


# --------------------------------------------------------------------------- #
# Telling the clients
# --------------------------------------------------------------------------- #


@respx.mock
async def test_list_changed_fires_when_the_tool_list_actually_moved(
    session: AsyncSession, cipher: CredentialCipher
) -> None:
    serves(respx.mock, V1)
    server = await a_server(session, cipher)
    told = Announcements()

    serves(respx.mock, V2)
    report = await refresh.refresh_server(session, server.id, cipher=cipher, announce=told)

    # ``POST /pets`` was selected and is now removed, so a client's copy is stale.
    assert report.tools_changed is True
    assert told.count == 1


@respx.mock
async def test_list_changed_does_not_fire_on_a_no_op_refresh(
    session: AsyncSession, cipher: CredentialCipher
) -> None:
    serves(respx.mock, V1)
    server = await a_server(session, cipher)
    told = Announcements()

    serves(respx.mock, V1)
    report = await refresh.refresh_server(session, server.id, cipher=cipher, announce=told)

    assert report.outcome == "unchanged"
    assert report.tools_changed is False
    assert told.count == 0


@respx.mock
async def test_list_changed_does_not_fire_for_a_change_no_client_can_see(
    session: AsyncSession, cipher: CredentialCipher
) -> None:
    """A document that only grew an endpoint changes nothing a client is holding.

    The new operation is unselected, so ``tools/list`` answers exactly what it
    answered before. The server is flagged for review, and no client is woken up
    for a tool that does not exist yet.
    """
    serves(respx.mock, V1)
    server = await a_server(session, cipher)
    grown = copy.deepcopy(V1)
    grown["paths"]["/toys"] = V2["paths"]["/toys"]
    told = Announcements()

    serves(respx.mock, grown)
    report = await refresh.refresh_server(session, server.id, cipher=cipher, announce=told)

    assert report.counts["new"] == 1
    assert report.needs_attention is True
    assert report.tools_changed is False
    assert told.count == 0


@respx.mock
async def test_a_reworded_summary_on_a_live_tool_does_fire(
    session: AsyncSession, cipher: CredentialCipher
) -> None:
    """The description a model reads is part of the tool list.

    Nothing about this operation's *schema* moved, so it is not ``changed``; the
    sentence a model chooses tools by did move, so clients are told.
    """
    serves(respx.mock, V1)
    server = await a_server(session, cipher)
    reworded = copy.deepcopy(V1)
    reworded["paths"]["/pets"]["get"]["summary"] = "Every pet we have"
    told = Announcements()

    serves(respx.mock, reworded)
    report = await refresh.refresh_server(session, server.id, cipher=cipher, announce=told)

    assert report.counts == {"new": 0, "changed": 0, "removed": 0, "restored": 0}
    assert report.tools_changed is True
    assert told.count == 1


@respx.mock
async def test_a_failed_refresh_tells_nobody_anything(
    session: AsyncSession, cipher: CredentialCipher
) -> None:
    serves(respx.mock, V1)
    server = await a_server(session, cipher)
    told = Announcements()

    respx.mock.get(SPEC_URL).mock(return_value=httpx.Response(500))
    report = await refresh.refresh_server(session, server.id, cipher=cipher, announce=told)

    assert report.outcome == "failed"
    assert told.count == 0


@respx.mock
async def test_a_disabled_server_contributes_no_tools_so_nothing_is_announced(
    session: AsyncSession, cipher: CredentialCipher
) -> None:
    """A refresh of a disabled server is allowed, and invisible to every client."""
    serves(respx.mock, V1)
    server = await a_server(session, cipher)
    await repo.set_server_enabled(session, server.id, enabled=False)
    await session.commit()
    told = Announcements()

    serves(respx.mock, V2)
    report = await refresh.refresh_server(session, server.id, cipher=cipher, announce=told)

    assert report.outcome == "updated"
    assert report.tools_changed is False
    assert told.count == 0


# --------------------------------------------------------------------------- #
# The signature the announcement is decided by
# --------------------------------------------------------------------------- #


@respx.mock
async def test_the_signature_covers_only_what_a_client_is_shown(
    session: AsyncSession, cipher: CredentialCipher
) -> None:
    serves(respx.mock, V1)
    server = await a_server(session, cipher, selection=(LIST_PETS,))
    before = await refresh.tool_signature(session)

    # Unselected operations are not in the list, so their rows are not in it
    # either — and neither is anything about the server that is not advertised.
    await repo.update_server(
        session, server.id, repo.ServerPatch(name="Petstore EU"), cipher=cipher
    )
    unlisted = (await rows(session, server.id))[ADD_PET]
    await repo.update_operation(
        session, unlisted.id, repo.OperationPatch(description_override="Never shown.")
    )
    await session.commit()

    assert await refresh.tool_signature(session) != before, "the server name is in the origin line"
    assert len(await refresh.tool_signature(session)) == 1


@respx.mock
async def test_the_spec_credential_is_the_stored_one_and_is_actually_sent(
    session: AsyncSession, cipher: CredentialCipher
) -> None:
    """A refresh authenticates the download with what the row holds (spec §5.1)."""
    keyed = respx.mock.get(SPEC_URL).mock(
        side_effect=lambda request: (
            httpx.Response(200, json=V1)
            if request.headers.get("X-Spec-Key") == SPEC_KEY
            else httpx.Response(401)
        )
    )
    server = await a_server(
        session,
        cipher,
        spec_credential=ApiKeyCredential(header="X-Spec-Key", value=SPEC_KEY),
    )

    report = await refresh.refresh_server(session, server.id, cipher=cipher)

    assert report.outcome == "unchanged"
    assert keyed.call_count == 2  # the register, and the refresh
    assert all(call.request.headers.get("X-Spec-Key") == SPEC_KEY for call in keyed.calls)


# --------------------------------------------------------------------------- #
# The record it leaves
# --------------------------------------------------------------------------- #


@respx.mock
async def test_an_updated_refresh_stores_the_new_document_and_its_hash(
    session: AsyncSession, cipher: CredentialCipher
) -> None:
    serves(respx.mock, V1)
    server = await a_server(session, cipher)
    was = server.spec_hash

    serves(respx.mock, V2)
    report = await refresh.refresh_server(session, server.id, cipher=cipher)

    stored = await repo.require_server(session, server.id)
    assert stored.spec_hash == report.spec_hash != was
    assert report.previous_hash == was
    assert "/toys" in (stored.spec_snapshot or {}).get("paths", {})


@respx.mock
async def test_a_refresh_leaves_an_overridden_base_url_alone(
    session: AsyncSession, cipher: CredentialCipher
) -> None:
    """The document says where the API lives; the operator gets the last word."""
    serves(respx.mock, V1)
    server = await a_server(session, cipher)
    await repo.update_server(
        session,
        server.id,
        repo.ServerPatch(base_url="https://eu.petstore.example/v2"),
        cipher=cipher,
    )
    await session.commit()

    serves(respx.mock, V2)
    await refresh.refresh_server(session, server.id, cipher=cipher)

    assert (await repo.require_server(session, server.id)).base_url == (
        "https://eu.petstore.example/v2"
    )


@respx.mock
async def test_the_report_reads_as_a_sentence(
    session: AsyncSession, cipher: CredentialCipher
) -> None:
    serves(respx.mock, V1)
    server = await a_server(session, cipher)

    serves(respx.mock, V2)
    updated = await refresh.refresh_server(session, server.id, cipher=cipher)
    unchanged = await refresh.refresh_server(session, server.id, cipher=cipher)

    assert updated.summary == "Petstore: 1 new, 1 changed, 1 removed."
    assert unchanged.summary == "Petstore is unchanged."
    assert updated.ok and unchanged.ok


@respx.mock
async def test_a_refresh_commits_before_it_announces(
    session: AsyncSession, cipher: CredentialCipher, database: Database
) -> None:
    """A client that refetches on hearing the news must find the new list.

    Asserted from a second session, opened while the announcement is being made:
    if the write were still uncommitted, this would read the old tool list.
    """
    serves(respx.mock, V1)
    server = await a_server(session, cipher)
    seen: list[list[str]] = []

    async def look() -> None:
        async with database.session_factory() as other:
            seen.append(await tool_names(other))

    serves(respx.mock, V2)
    await refresh.refresh_server(session, server.id, cipher=cipher, announce=look)

    assert seen == [["petstore__getPet", "petstore__listPets"]]
