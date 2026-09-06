"""How ingestion tells the operator that a document was not quite right.

Turning a spec URL into tools is a pipeline — fetch, resolve refs, convert,
normalise, extract — and every stage of it meets documents that are wrong in
some way. There are only two useful answers to that. Either the stage can carry
on with something degraded, and the operator should be told what was lost: a
:class:`SpecWarning`. Or it cannot, and the import stops: a :class:`SpecError`.

Both live here rather than in the stage that produces them, because the UI shows
them side by side and the stages have to agree on their shape.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SpecWarning:
    """One thing the gateway had to do differently than the document asked."""

    #: Stable and machine-readable, so the UI can group and the tests can name
    #: what they are asserting without matching on prose.
    code: str
    #: A sentence for whoever has to decide whether they care.
    message: str
    #: JSON pointer to where in the document it happened, when there is a
    #: single place to point at.
    location: str | None = None


class Diagnostics:
    """The warnings raised while one pass reads one document.

    Deduplicated on ``(code, message)``. A recursive schema referenced from
    eighty operations is one thing wrong with the document, not eighty, and a
    warning list nobody can read is a warning list nobody reads. The first
    location wins, because it is the one an operator can go and look at.
    """

    def __init__(self) -> None:
        self._seen: dict[tuple[str, str], SpecWarning] = {}

    def add(self, code: str, message: str, *, location: str | None = None) -> None:
        self._seen.setdefault(
            (code, message), SpecWarning(code=code, message=message, location=location)
        )

    @property
    def warnings(self) -> tuple[SpecWarning, ...]:
        """Everything collected, in the order it was first noticed."""
        return tuple(self._seen.values())

    def __iter__(self) -> Iterator[SpecWarning]:
        return iter(self._seen.values())

    def __len__(self) -> int:
        return len(self._seen)


class SpecError(Exception):
    """A spec could not be turned into a working set of tools.

    The root of everything ingestion raises, including the fetch failures in
    :mod:`mcp_gateway.openapi.fetch`, so the UI has one thing to catch when all
    it wants to do is put the reason on the page.
    """


class UnresolvedRefError(SpecError):
    """A ``$ref`` points at something the document does not contain.

    Not a degradation: a pointer into thin air means the document is broken in
    a way the author has to fix, and guessing at what they meant would only
    move the confusion further downstream.
    """

    def __init__(self, ref: str, *, location: str | None = None) -> None:
        self.ref = ref
        self.location = location
        where = f", referenced from {location}" if location else ""
        super().__init__(
            f"The $ref {ref!r} points at something the document does not contain{where}."
        )


class UnsupportedSpecVersionError(SpecError):
    """The document is not a spec version the gateway knows how to read.

    Swagger 2.0, OpenAPI 3.0 and OpenAPI 3.1 are the three the gateway speaks.
    Anything else — Swagger 1.x, a version that does not exist yet, a JSON file
    that is not a spec at all — stops here rather than being guessed at, because
    every stage downstream is written against a shape this one has confirmed.
    """

    def __init__(self, found: str | None) -> None:
        self.found = found
        what = (
            f"declares itself as {found!r}"
            if found
            else "has no 'openapi' or 'swagger' version key"
        )
        super().__init__(
            f"This document {what}. The gateway reads Swagger 2.0, OpenAPI 3.0 and OpenAPI 3.1."
        )


__all__ = [
    "Diagnostics",
    "SpecError",
    "SpecWarning",
    "UnresolvedRefError",
    "UnsupportedSpecVersionError",
]
