"""What a tool is called, and what happens when two of them want one name.

The tests split the way the module does: the first half never touches a
database, because building a name is arithmetic on strings; the second half
does, because "is this name taken" is a question only the whole table can
answer.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select

from mcp_gateway.config import load_settings
from mcp_gateway.crypto import CredentialCipher, generate_key
from mcp_gateway.db import repo
from mcp_gateway.db.models import Base, Operation, Server
from mcp_gateway.db.repo import NewServer, OperationInput, ServerNotFound
from mcp_gateway.db.session import Database, open_database
from mcp_gateway.naming import (
    DIGEST_LENGTH,
    MAX_SLUG,
    MAX_TOOL_NAME,
    NameAssignment,
    NameConflict,
    NamedOperation,
    NamePlan,
    ToolOwner,
    check_conflicts,
    default_tool_name,
    is_legal_tool_name,
    path_slug,
    plan_names,
    plan_tool_names,
    rename_server,
    sanitize,
    server_slug,
    tool_name,
)
from mcp_gateway.openapi.schema import extract_operations

# --------------------------------------------------------------------------- #
# Fixtures and small builders
# --------------------------------------------------------------------------- #


@pytest.fixture
async def database(tmp_path: Path) -> AsyncIterator[Database]:
    """A real SQLite file, so the unique index is a real unique index."""
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


def an_op(
    op_key: str = "GET /users",
    *,
    operation_id: str | None = "getUser",
    override: str | None = None,
    current_name: str | None = None,
) -> NamedOperation:
    method, path = op_key.split(" ", 1)
    return NamedOperation(
        op_key=op_key,
        method=method,
        path=path,
        operation_id=operation_id,
        override=override,
        current_name=current_name,
    )


async def registered(
    session: Any,
    cipher: CredentialCipher,
    slug: str = "petstore",
    *ops: NamedOperation,
    **overrides: Any,
) -> Server:
    """A saved server whose operations already carry their planned names."""
    server = await repo.create_server(session, a_server(slug, **overrides), cipher=cipher)
    plan = await plan_tool_names(
        session, ops, prefix=server.tool_prefix, server_name=server.name, server_id=server.id
    )
    await repo.upsert_operations(
        session,
        server.id,
        [
            OperationInput(
                op_key=op.op_key,
                operation_id=op.operation_id,
                method=op.method,
                path=op.path,
                input_schema={"type": "object", "properties": {}},
                input_schema_hash=f"hash-{op.op_key}",
                tool_name=plan.names[op.op_key],
            )
            for op in ops
        ],
    )
    return server


async def names_of(session: Any, server_id: int) -> dict[str, str]:
    """``op_key`` → the name actually written, read back from the database."""
    rows = await session.scalars(
        select(Operation)
        .where(Operation.server_id == server_id)
        .execution_options(populate_existing=True)
    )
    return {row.op_key: row.effective_tool_name for row in rows}


# --------------------------------------------------------------------------- #
# The four acceptance criteria
# --------------------------------------------------------------------------- #


async def test_two_servers_exposing_get_user_get_different_names(
    session: Any, cipher: CredentialCipher
) -> None:
    """The prefix is the whole reason a gateway can front two APIs at once."""
    petstore = await registered(session, cipher, "petstore", an_op())
    billing = await registered(session, cipher, "billing", an_op())

    assert await names_of(session, petstore.id) == {"GET /users": "petstore__getUser"}
    assert await names_of(session, billing.id) == {"GET /users": "billing__getUser"}


async def test_an_override_that_collides_names_both_sides(
    session: Any, cipher: CredentialCipher
) -> None:
    """The message has to be actionable: which two operations, on which servers."""
    await registered(session, cipher, "petstore", an_op("GET /users"))
    billing = await registered(session, cipher, "billing", an_op("GET /invoices", operation_id="a"))

    plan = await rename_server(
        session, billing.id, overrides={"GET /invoices": "petstore__getUser"}
    )

    assert not plan.ok
    conflict = plan.conflicts[0]
    assert conflict.name == "petstore__getUser"
    assert (conflict.holder.op_key, conflict.holder.server_name) == ("GET /users", "Petstore")
    assert (conflict.claimant.op_key, conflict.claimant.server_name) == ("GET /invoices", "Billing")
    assert "GET /users on Petstore" in conflict.message
    assert "GET /invoices on Billing" in conflict.message


def test_a_very_long_operation_id_truncates_the_same_way_every_time() -> None:
    long_id = "get" + "Extremely" * 30 + "LongThing"
    name = default_tool_name("petstore", operation_id=long_id)

    assert len(name) == MAX_TOOL_NAME
    assert is_legal_tool_name(name)
    assert name.startswith("petstore__get")
    assert name == default_tool_name("petstore", operation_id=long_id)


def test_two_long_ids_that_start_alike_still_get_two_tools() -> None:
    """Truncation keeps the head, so only the digest can tell these apart."""
    shared = "get" + "Extremely" * 30
    first = default_tool_name("petstore", operation_id=f"{shared}Pets")
    second = default_tool_name("petstore", operation_id=f"{shared}Photos")

    assert first[:-DIGEST_LENGTH] == second[:-DIGEST_LENGTH]
    assert first != second


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("get user", "petstore__get_user"),
        ("pets/getById", "petstore__pets_getById"),
        ("get{petId}", "petstore__get_petId"),
        # Nothing legal survives, so the route names it instead.
        ("списокПитомцев", "petstore__get_root"),
        ("get-user", "petstore__get-user"),
        ("café", "petstore__caf"),
    ],
)
def test_an_operation_id_is_sanitised_into_the_legal_set(raw: str, expected: str) -> None:
    name = default_tool_name("petstore", operation_id=raw)
    assert name == expected
    assert is_legal_tool_name(name)


@pytest.mark.parametrize(
    "raw",
    ["get user", "pets/getById", "get{petId}", "a b\tc\nd", "emoji-🐈-here", "a/b/c"],
)
def test_whatever_a_spec_spells_it_the_name_is_legal(raw: str) -> None:
    assert is_legal_tool_name(default_tool_name("my prefix!", operation_id=raw))
    assert is_legal_tool_name(tool_name("my prefix!", override=raw, path="/x"))


# --------------------------------------------------------------------------- #
# Building one name
# --------------------------------------------------------------------------- #


def test_a_spec_without_an_operation_id_is_named_from_its_route() -> None:
    assert (
        default_tool_name("petstore", method="GET", path="/pets/{petId}/photos")
        == "petstore__get_pets_petId_photos"
    )


def test_the_method_is_part_of_that_name() -> None:
    """Two verbs on one path are two tools, and must not be one name."""
    get = default_tool_name("petstore", method="GET", path="/pets")
    post = default_tool_name("petstore", method="POST", path="/pets")
    assert get != post
    assert (get, post) == ("petstore__get_pets", "petstore__post_pets")


def test_the_root_path_still_has_a_name() -> None:
    assert default_tool_name("petstore", method="GET", path="/") == "petstore__get_root"


def test_an_operation_id_wins_over_the_route() -> None:
    assert (
        default_tool_name("petstore", operation_id="listPets", method="GET", path="/pets")
        == "petstore__listPets"
    )


def test_an_operation_id_keeps_its_own_spelling() -> None:
    """Including runs of underscores, which are somebody's deliberate choice."""
    assert default_tool_name("petstore", operation_id="get__user") == "petstore__get__user"


def test_a_path_collapses_the_runs_its_punctuation_leaves() -> None:
    assert path_slug("/pets/{petId}/photos") == "pets_petId_photos"
    assert path_slug("//pets//") == "pets"


def test_an_override_replaces_the_whole_name() -> None:
    """Prefix included: the operator said what they want it called."""
    assert tool_name("petstore", operation_id="getUser", override="whoami") == "whoami"


def test_a_blank_override_is_no_override() -> None:
    """A cleared form field arrives as an empty string as often as as None."""
    for blank in ("", "   ", "\t"):
        assert tool_name("petstore", operation_id="getUser", override=blank) == "petstore__getUser"


def test_an_override_of_nothing_but_punctuation_falls_back_too() -> None:
    assert tool_name("petstore", operation_id="getUser", override="///") == "petstore__getUser"


def test_an_override_is_sanitised_like_anything_else() -> None:
    assert tool_name("petstore", override="who am i", path="/x") == "who_am_i"


def test_a_server_with_no_usable_prefix_still_names_its_tools() -> None:
    """No prefix rather than a name that opens with the separator."""
    assert default_tool_name("", operation_id="getUser") == "getUser"
    assert default_tool_name("!!!", operation_id="getUser") == "getUser"


def test_an_operation_that_offers_nothing_is_named_from_its_method() -> None:
    assert default_tool_name("petstore", operation_id="???", method="?", path="?") == (
        "petstore__call_root"
    )


def test_a_name_that_fits_exactly_is_left_alone() -> None:
    """The digest is for names that do not fit, not for names that nearly do."""
    stem = "x" * (MAX_TOOL_NAME - len("petstore__"))
    assert default_tool_name("petstore", operation_id=stem) == f"petstore__{stem}"


def test_a_truncated_name_keeps_the_prefix_that_identifies_the_server() -> None:
    long_id = "z" * 400
    first = default_tool_name("petstore", operation_id=long_id)
    second = default_tool_name("billing", operation_id=long_id)

    assert first.startswith("petstore__")
    assert second.startswith("billing__")
    assert first != second


def test_a_long_override_is_truncated_too() -> None:
    name = tool_name("petstore", override="q" * 400, path="/x")
    assert len(name) == MAX_TOOL_NAME
    assert is_legal_tool_name(name)


@pytest.mark.parametrize(
    ("name", "legal"),
    [
        ("petstore__getUser", True),
        ("a", True),
        ("a-b_c-9", True),
        ("", False),
        ("has space", False),
        ("has/slash", False),
        ("x" * MAX_TOOL_NAME, True),
        ("x" * (MAX_TOOL_NAME + 1), False),
    ],
)
def test_what_counts_as_a_legal_name(name: str, legal: bool) -> None:
    assert is_legal_tool_name(name) is legal


def test_sanitize_leaves_a_legal_fragment_untouched() -> None:
    assert sanitize("getUser-2") == "getUser-2"


def test_sanitize_cannot_produce_a_fake_separator_at_an_edge() -> None:
    """Otherwise a stem could look like it carried a prefix of its own."""
    assert sanitize("__getUser__") == "getUser"
    assert sanitize("///getUser///") == "getUser"


# --------------------------------------------------------------------------- #
# Planning a batch
# --------------------------------------------------------------------------- #


def test_a_plan_names_every_operation_it_is_given() -> None:
    plan = plan_names(
        [an_op("GET /users"), an_op("POST /users", operation_id="createUser")],
        prefix="petstore",
        server_name="Petstore",
    )

    assert plan.ok
    assert plan.names == {
        "GET /users": "petstore__getUser",
        "POST /users": "petstore__createUser",
    }


def test_two_operations_of_one_server_wanting_one_name_is_a_conflict() -> None:
    """A spec is free to repeat an operationId; the gateway is not."""
    plan = plan_names(
        [an_op("GET /users"), an_op("GET /people")],
        prefix="petstore",
        server_name="Petstore",
    )

    assert not plan.ok
    conflict = plan.conflicts[0]
    assert conflict.holder.op_key == "GET /users"
    assert conflict.claimant.op_key == "GET /people"


def test_the_loser_of_a_conflict_still_appears_in_the_plan() -> None:
    """So the UI renders the whole table with the clash marked, not a hole."""
    plan = plan_names(
        [an_op("GET /users"), an_op("GET /people")],
        prefix="petstore",
        server_name="Petstore",
    )

    assert [assignment.op_key for assignment in plan.assignments] == ["GET /users", "GET /people"]
    assert {assignment.name for assignment in plan.assignments} == {"petstore__getUser"}


def test_the_first_operation_asked_keeps_the_name() -> None:
    """Order is the caller's, and for ingestion it is document order."""
    plan = plan_names(
        [an_op("GET /people"), an_op("GET /users")],
        prefix="petstore",
        server_name="Petstore",
    )
    assert plan.conflicts[0].holder.op_key == "GET /people"


def test_a_plan_knows_which_names_would_change() -> None:
    plan = plan_names(
        [
            an_op("GET /users", current_name="old__getUser"),
            an_op("POST /users", operation_id="createUser", current_name="petstore__createUser"),
            an_op("DELETE /users", operation_id="deleteUser"),
        ],
        prefix="petstore",
        server_name="Petstore",
    )

    assert [change.op_key for change in plan.changes] == ["GET /users"]
    assert [a.op_key for a in plan.assignments if a.is_new] == ["DELETE /users"]


def test_a_plan_with_nothing_in_it_is_fine() -> None:
    plan = plan_names([], prefix="petstore", server_name="Petstore")
    assert plan.ok
    assert plan.names == {}


def test_conflicts_added_later_join_the_ones_already_there() -> None:
    plan = NamePlan(
        server_name="Petstore",
        assignments=(NameAssignment(op_key="GET /users", name="x"),),
        conflicts=(
            NameConflict(
                name="x",
                holder=ToolOwner(server_name="A", op_key="GET /a"),
                claimant=ToolOwner(server_name="Petstore", op_key="GET /users"),
            ),
        ),
    )
    same = plan.conflicts[0]
    other = NameConflict(
        name="x",
        holder=ToolOwner(server_name="B", op_key="GET /b"),
        claimant=ToolOwner(server_name="Petstore", op_key="GET /users"),
    )

    assert plan.with_conflicts([same, other]).conflicts == (same, other)


def test_a_named_operation_can_be_built_from_an_extracted_one() -> None:
    document = {
        "openapi": "3.1.0",
        "paths": {"/pets": {"get": {"operationId": "listPets", "responses": {}}}},
    }
    extracted = extract_operations(document)
    named = NamedOperation.from_extracted(extracted.operations[0])

    assert named == NamedOperation(
        op_key="GET /pets", method="GET", path="/pets", operation_id="listPets"
    )
    assert tool_name("petstore", operation_id=named.operation_id) == "petstore__listPets"


# --------------------------------------------------------------------------- #
# Checking against everything already stored
# --------------------------------------------------------------------------- #


async def test_a_name_free_everywhere_raises_nothing(
    session: Any, cipher: CredentialCipher
) -> None:
    await registered(session, cipher, "petstore", an_op("GET /users"))

    plan = await plan_tool_names(
        session,
        [an_op("GET /invoices", operation_id="listInvoices")],
        prefix="billing",
        server_name="Billing",
    )
    assert plan.ok


async def test_a_server_being_added_is_checked_against_every_stored_name(
    session: Any, cipher: CredentialCipher
) -> None:
    """The wizard has no server id yet, so nothing is excluded from the check."""
    await registered(session, cipher, "petstore", an_op("GET /users"))

    plan = await plan_tool_names(
        session,
        [an_op("GET /accounts", override="petstore__getUser")],
        prefix="billing",
        server_name="Billing",
    )

    assert not plan.ok
    assert plan.conflicts[0].holder.server_name == "Petstore"
    assert plan.conflicts[0].claimant.server_id is None


async def test_a_servers_own_rows_do_not_conflict_with_themselves(
    session: Any, cipher: CredentialCipher
) -> None:
    """A refresh proposes the names the rows already have; that is not a clash."""
    server = await registered(session, cipher, "petstore", an_op("GET /users"))

    plan = await plan_tool_names(
        session,
        [an_op("GET /users", current_name="petstore__getUser")],
        prefix="petstore",
        server_name="Petstore",
        server_id=server.id,
    )
    assert plan.ok


async def test_an_unselected_operation_still_holds_its_name(
    session: Any, cipher: CredentialCipher
) -> None:
    """The unique index covers the whole table, so the check has to as well."""
    await registered(session, cipher, "petstore", an_op("GET /users"))
    assert all(not row.selected for row in await repo.list_operations(session, 1))

    plan = await plan_tool_names(
        session,
        [an_op("GET /accounts", override="petstore__getUser")],
        prefix="billing",
        server_name="Billing",
    )
    assert not plan.ok


async def test_a_removed_operation_still_holds_its_name(
    session: Any, cipher: CredentialCipher
) -> None:
    server = await registered(session, cipher, "petstore", an_op("GET /users"))
    await repo.upsert_operations(session, server.id, [])
    assert (await repo.list_operations(session, server.id))[0].status == "removed"

    plan = await plan_tool_names(
        session,
        [an_op("GET /accounts", override="petstore__getUser")],
        prefix="billing",
        server_name="Billing",
    )
    assert not plan.ok


async def test_checking_nothing_asks_the_database_nothing(session: Any) -> None:
    plan = plan_names([], prefix="petstore", server_name="Petstore")
    assert await check_conflicts(session, plan) == ()


async def test_a_big_server_is_checked_in_batches(session: Any, cipher: CredentialCipher) -> None:
    """More names than one ``IN`` clause should carry, and one of them taken."""
    stored = [an_op(f"GET /r{index}", operation_id=f"op{index}") for index in range(1000)]
    await registered(session, cipher, "petstore", *stored)

    plan = await plan_tool_names(
        session,
        [*stored, an_op("GET /extra", override="petstore__op999")],
        prefix="billing",
        server_name="Billing",
    )

    assert len(plan.assignments) == 1001
    assert [conflict.name for conflict in plan.conflicts] == ["petstore__op999"]
    assert plan.conflicts[0].holder.op_key == "GET /r999"


# --------------------------------------------------------------------------- #
# Recomputing a stored server
# --------------------------------------------------------------------------- #


async def test_a_new_prefix_renames_every_generated_name(
    session: Any, cipher: CredentialCipher
) -> None:
    server = await registered(
        session,
        cipher,
        "petstore",
        an_op("GET /users"),
        an_op("GET /pets", operation_id="listPets"),
    )

    plan = await rename_server(session, server.id, prefix="zoo")

    assert plan.ok
    assert await names_of(session, server.id) == {
        "GET /users": "zoo__getUser",
        "GET /pets": "zoo__listPets",
    }


async def test_a_dry_run_writes_nothing(session: Any, cipher: CredentialCipher) -> None:
    server = await registered(session, cipher, "petstore", an_op("GET /users"))

    plan = await rename_server(session, server.id, prefix="zoo", dry_run=True)

    assert plan.ok
    assert [(c.op_key, c.current_name, c.name) for c in plan.changes] == [
        ("GET /users", "petstore__getUser", "zoo__getUser")
    ]
    assert await names_of(session, server.id) == {"GET /users": "petstore__getUser"}


async def test_a_dry_run_and_the_real_thing_agree(session: Any, cipher: CredentialCipher) -> None:
    """The preview is only worth showing if it is what happens next."""
    server = await registered(
        session,
        cipher,
        "petstore",
        an_op("GET /users"),
        an_op("GET /pets", operation_id="listPets"),
    )

    preview = await rename_server(session, server.id, prefix="zoo", dry_run=True)
    applied = await rename_server(session, server.id, prefix="zoo")

    assert preview.names == applied.names
    assert applied.names == await names_of(session, server.id)


async def test_a_prefix_change_that_would_collide_writes_nothing(
    session: Any, cipher: CredentialCipher
) -> None:
    await registered(session, cipher, "zoo", an_op("GET /users"))
    petstore = await registered(session, cipher, "petstore", an_op("GET /users"))

    plan = await rename_server(session, petstore.id, prefix="zoo")

    assert not plan.ok
    assert await names_of(session, petstore.id) == {"GET /users": "petstore__getUser"}


async def test_a_conflict_stops_the_names_that_would_have_been_fine_too(
    session: Any, cipher: CredentialCipher
) -> None:
    """All or nothing: half a rename is a server nobody can reason about."""
    await registered(session, cipher, "zoo", an_op("GET /users"))
    petstore = await registered(
        session,
        cipher,
        "petstore",
        an_op("GET /users"),
        an_op("GET /pets", operation_id="listPets"),
    )

    await rename_server(session, petstore.id, prefix="zoo")

    assert await names_of(session, petstore.id) == {
        "GET /users": "petstore__getUser",
        "GET /pets": "petstore__listPets",
    }


async def test_setting_an_override_renames_one_tool(session: Any, cipher: CredentialCipher) -> None:
    server = await registered(
        session,
        cipher,
        "petstore",
        an_op("GET /users"),
        an_op("GET /pets", operation_id="listPets"),
    )

    plan = await rename_server(session, server.id, overrides={"GET /users": "whoami"})

    assert plan.ok
    assert await names_of(session, server.id) == {
        "GET /users": "whoami",
        "GET /pets": "petstore__listPets",
    }


async def test_an_override_is_stored_alongside_the_name_it_produced(
    session: Any, cipher: CredentialCipher
) -> None:
    server = await registered(session, cipher, "petstore", an_op("GET /users"))

    await rename_server(session, server.id, overrides={"GET /users": "whoami"})

    row = (await repo.list_operations(session, server.id))[0]
    assert (row.tool_name_override, row.effective_tool_name) == ("whoami", "whoami")


async def test_clearing_an_override_restores_the_generated_default(
    session: Any, cipher: CredentialCipher
) -> None:
    server = await registered(session, cipher, "petstore", an_op("GET /users"))
    await rename_server(session, server.id, overrides={"GET /users": "whoami"})

    await rename_server(session, server.id, overrides={"GET /users": None})

    row = (await repo.list_operations(session, server.id))[0]
    assert (row.tool_name_override, row.effective_tool_name) == (None, "petstore__getUser")


async def test_a_blank_override_clears_it_as_well(session: Any, cipher: CredentialCipher) -> None:
    server = await registered(session, cipher, "petstore", an_op("GET /users"))
    await rename_server(session, server.id, overrides={"GET /users": "whoami"})

    await rename_server(session, server.id, overrides={"GET /users": "  "})

    row = (await repo.list_operations(session, server.id))[0]
    assert (row.tool_name_override, row.effective_tool_name) == (None, "petstore__getUser")


async def test_an_operation_left_out_of_the_mapping_keeps_its_override(
    session: Any, cipher: CredentialCipher
) -> None:
    """Absent is not the same as ``None``, which is why this takes a mapping."""
    server = await registered(
        session,
        cipher,
        "petstore",
        an_op("GET /users"),
        an_op("GET /pets", operation_id="listPets"),
    )
    await rename_server(session, server.id, overrides={"GET /users": "whoami"})

    await rename_server(session, server.id, overrides={"GET /pets": "pets"})

    rows = {row.op_key: row for row in await repo.list_operations(session, server.id)}
    assert rows["GET /users"].tool_name_override == "whoami"
    assert rows["GET /users"].effective_tool_name == "whoami"


async def test_an_override_keeps_its_name_when_the_prefix_moves(
    session: Any, cipher: CredentialCipher
) -> None:
    """That is the point of an override: it is not derived from anything."""
    server = await registered(
        session,
        cipher,
        "petstore",
        an_op("GET /users"),
        an_op("GET /pets", operation_id="listPets"),
    )
    await rename_server(session, server.id, overrides={"GET /users": "whoami"})

    await rename_server(session, server.id, prefix="zoo")

    assert await names_of(session, server.id) == {
        "GET /users": "whoami",
        "GET /pets": "zoo__listPets",
    }


async def test_an_override_colliding_with_a_sibling_is_caught(
    session: Any, cipher: CredentialCipher
) -> None:
    """A check scoped to "some other server" would have missed this one."""
    server = await registered(
        session,
        cipher,
        "petstore",
        an_op("GET /users"),
        an_op("GET /pets", operation_id="listPets"),
    )

    plan = await rename_server(session, server.id, overrides={"GET /pets": "petstore__getUser"})

    assert not plan.ok
    assert plan.conflicts[0].holder.op_key == "GET /users"
    assert plan.conflicts[0].claimant.op_key == "GET /pets"
    assert await names_of(session, server.id) == {
        "GET /users": "petstore__getUser",
        "GET /pets": "petstore__listPets",
    }


async def test_two_tools_can_trade_names(session: Any, cipher: CredentialCipher) -> None:
    """The unique index would refuse this halfway through a naive write."""
    server = await registered(
        session,
        cipher,
        "petstore",
        an_op("GET /users"),
        an_op("GET /pets", operation_id="listPets"),
    )
    await rename_server(session, server.id, overrides={"GET /users": "alpha", "GET /pets": "beta"})

    plan = await rename_server(
        session, server.id, overrides={"GET /users": "beta", "GET /pets": "alpha"}
    )

    assert plan.ok
    assert await names_of(session, server.id) == {"GET /users": "beta", "GET /pets": "alpha"}


async def test_renaming_a_server_that_is_not_there(session: Any) -> None:
    with pytest.raises(ServerNotFound):
        await rename_server(session, 404, prefix="zoo")


async def test_a_server_with_no_operations_renames_to_nothing(
    session: Any, cipher: CredentialCipher
) -> None:
    server = await registered(session, cipher, "petstore")
    plan = await rename_server(session, server.id, prefix="zoo")
    assert plan.ok
    assert plan.assignments == ()


async def test_renaming_does_not_touch_the_servers_prefix(
    session: Any, cipher: CredentialCipher
) -> None:
    """The caller writes that, in the same transaction, so both land together."""
    server = await registered(session, cipher, "petstore", an_op("GET /users"))

    await rename_server(session, server.id, prefix="zoo")

    assert server.tool_prefix == "petstore"


async def test_a_rename_reaches_the_tool_list(session: Any, cipher: CredentialCipher) -> None:
    """The name in the plan is the name a model is told about."""
    server = await registered(session, cipher, "petstore", an_op("GET /users"))
    await repo.set_selected(session, server.id, ["GET /users"])

    await rename_server(session, server.id, overrides={"GET /users": "whoami"})

    assert [row.tool_name for row in await repo.list_tools(session)] == ["whoami"]
    assert await repo.get_tool(session, "whoami") is not None
    assert await repo.get_tool(session, "petstore__getUser") is None


# --------------------------------------------------------------------------- #
# A server's own slug
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Petstore", "petstore"),
        ("Pet Store", "pet_store"),
        ("Pet  Store!!", "pet_store"),
        ("ACME Billing (v2)", "acme_billing_v2"),
        ("internal-api", "internal-api"),
        ("???", ""),
    ],
)
def test_a_display_name_becomes_an_identifier(name: str, expected: str) -> None:
    # The default for both ``slug`` and ``tool_prefix`` (spec §4). Lower case,
    # because a prefix differing from another only in case reads as the same
    # server to the person scanning a tool list.
    assert server_slug(name) == expected


def test_a_very_long_name_is_cut_to_what_the_column_holds() -> None:
    assert len(server_slug("a" * 300)) == MAX_SLUG


def test_a_slug_leads_the_names_of_the_server_it_belongs_to() -> None:
    # The point of having it here: what a server is called and what its tools
    # are called are decided by the same rule.
    assert default_tool_name(server_slug("Pet Store"), operation_id="listPets") == (
        "pet_store__listPets"
    )
