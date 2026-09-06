"""The two ways ingestion reports a document it did not entirely like."""

from __future__ import annotations

from mcp_gateway.openapi.diagnostics import (
    Diagnostics,
    SpecError,
    SpecWarning,
    UnresolvedRefError,
)
from mcp_gateway.openapi.fetch import SpecFetchError


def test_warnings_come_back_in_the_order_they_were_noticed() -> None:
    diagnostics = Diagnostics()

    diagnostics.add("first", "One thing.", location="/a")
    diagnostics.add("second", "Another thing.")

    assert [warning.code for warning in diagnostics.warnings] == ["first", "second"]
    assert diagnostics.warnings[1] == SpecWarning(code="second", message="Another thing.")
    assert len(diagnostics) == 2
    assert list(diagnostics) == list(diagnostics.warnings)


def test_the_same_thing_said_twice_is_recorded_once() -> None:
    # A shared schema is referenced from every operation in the document. It is
    # one problem, and a hundred copies of it is a list nobody reads.
    diagnostics = Diagnostics()

    diagnostics.add("ref_cycle", "It loops.", location="/components/schemas/Node")
    diagnostics.add("ref_cycle", "It loops.", location="/paths/~1pets/get")

    # The first location wins: it is the one worth going to look at.
    assert diagnostics.warnings == (
        SpecWarning(code="ref_cycle", message="It loops.", location="/components/schemas/Node"),
    )


def test_the_same_code_about_different_things_is_kept_apart() -> None:
    diagnostics = Diagnostics()

    diagnostics.add("external_ref", "'a.yaml#/X' is elsewhere.")
    diagnostics.add("external_ref", "'b.yaml#/Y' is elsewhere.")

    assert len(diagnostics) == 2


def test_a_fresh_collector_is_empty() -> None:
    assert Diagnostics().warnings == ()
    assert len(Diagnostics()) == 0


def test_every_ingestion_failure_shares_one_root() -> None:
    # The UI catches SpecError and puts the reason on the page, whether the URL
    # was unreachable or the document it served was broken.
    assert issubclass(SpecFetchError, SpecError)
    assert issubclass(UnresolvedRefError, SpecError)


def test_an_unresolved_ref_says_which_ref_and_where() -> None:
    error = UnresolvedRefError("#/components/schemas/Pet", location="/paths/~1pets/get")

    assert "#/components/schemas/Pet" in str(error)
    assert "/paths/~1pets/get" in str(error)


def test_an_unresolved_ref_reads_sensibly_without_a_location() -> None:
    error = UnresolvedRefError("#/components/schemas/Pet")

    assert str(error).endswith("does not contain.")
