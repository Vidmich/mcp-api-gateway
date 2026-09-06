"""Inlining ``$ref`` pointers into a document that stands on its own (spec §5.2).

Everything downstream — Swagger conversion, schema normalisation, the flat
inputSchema an MCP client is eventually handed — reads schemas by looking at
them. None of it should have to carry a pointer resolver around and remember to
use it, so refs are resolved once, here, and what comes out has no ``$ref`` left
in it anywhere.

Flattening a graph into a tree only terminates if something stops it, and specs
are full of things that would not stop on their own:

*Recursion.* ``TreeNode`` whose children are ``TreeNode``. The same pointer may
be expanded :data:`CYCLE_DEPTH` times in one chain; the next time it comes round
it becomes :data:`CUT_SCHEMA` and a warning. Eight is deeper than any argument a
model will actually construct, and shallow enough that the document stays a
document.

*Recursion that branches.* A schema with several recursive edges multiplies
rather than nests — six edges eight deep is over a million nodes — so
:data:`MAX_EXPANSIONS` caps the total. Hitting it means a pathological document,
not merely a big one, and it degrades the same way a cycle does instead of
taking the process with it.

*Refs into other files.* Not followed, by design. The fetcher went to one URL
with one credential the operator chose; going on to make further requests off
the back of what came back is not something anybody asked for. The ref becomes
:data:`PERMISSIVE_SCHEMA` and a warning, so the operation is still importable —
degraded, and labelled as degraded.

*Refs into nothing.* A pointer the document does not contain is not degradation,
it is a broken document, and it stops the import naming the pointer.
"""

from __future__ import annotations

import copy
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final

from mcp_gateway.openapi.diagnostics import Diagnostics, SpecWarning, UnresolvedRefError

#: How many times one pointer may be expanded within a single chain before the
#: chain is cut (spec §5.2).
CYCLE_DEPTH: Final = 8

#: Ceiling on inlined refs per document. Not a limit on document size — a
#: 5 MB spec of ordinary schemas is nowhere near it — but on combinatorial
#: recursion, which is the only thing that gets here.
MAX_EXPANSIONS: Final = 50_000

#: What a cut cycle leaves behind. Spec §5.2 names this shape: whatever was
#: going round the loop was an object, so an object is what is left.
CUT_SCHEMA: Final[dict[str, Any]] = {"type": "object"}

#: What a ref we will not follow leaves behind. Empty, rather than the cut
#: schema: a cycle is known to be an object because we were standing inside
#: one, while a ref into another file could have been anything at all, and a
#: schema that asserts nothing is the honest way to say we do not know.
PERMISSIVE_SCHEMA: Final[dict[str, Any]] = {}

#: Keys whose values are data rather than schemas. A ``{"$ref": "#/x"}`` sitting
#: under ``example`` is somebody's example of a JSON object that happens to have
#: that key, and inlining it would quietly corrupt the document. ``examples`` is
#: deliberately not in this set: in OpenAPI that one holds Example Objects,
#: which really can be refs.
OPAQUE_KEYS: Final = frozenset({"const", "default", "enum", "example"})

#: Warning codes, so the UI and the tests agree on them.
EXTERNAL_REF: Final = "external_ref"
REF_CYCLE: Final = "ref_cycle"
REF_BUDGET: Final = "ref_budget"


@dataclass(frozen=True, slots=True)
class ResolvedDocument:
    """A document with every internal ref inlined, and what that cost."""

    #: A new document. The one passed in is left exactly as it was.
    document: dict[str, Any]
    #: What had to be degraded to get here, for the UI to show. Empty for the
    #: documents most people bring.
    warnings: tuple[SpecWarning, ...]


def resolve_refs(document: Mapping[str, Any]) -> ResolvedDocument:
    """Inline every internal ``$ref`` in ``document``.

    Raises :class:`~mcp_gateway.openapi.diagnostics.UnresolvedRefError` if a
    pointer names something the document does not contain. Everything else a
    document can get wrong here comes back as a warning.
    """
    resolver = _Resolver(dict(document))
    # The root is walked key by key rather than handed to the resolver whole. A
    # document whose top level is itself a ``$ref`` is not an OpenAPI document,
    # and saying so is version detection's job (task 010), not this function's.
    resolved = {
        key: resolver.resolve(value, stack=(), location=f"/{_escape(str(key))}")
        for key, value in document.items()
    }
    return ResolvedDocument(document=resolved, warnings=resolver.diagnostics.warnings)


class _Resolver:
    """One pass over one document.

    Carries the three things the walk needs to share: the document refs point
    into, the warnings collected so far, and how much expanding has been done.
    """

    def __init__(self, root: dict[str, Any]) -> None:
        self._root = root
        self._expansions = 0
        self.diagnostics = Diagnostics()

    def resolve(self, node: Any, *, stack: tuple[str, ...], location: str) -> Any:
        """Rebuild ``node`` with its refs inlined.

        ``stack`` is the pointers currently being expanded, innermost last —
        which is what makes a cycle visible. ``location`` is where we are, as a
        JSON pointer, so a warning can say where to look.
        """
        if isinstance(node, dict):
            ref = _ref_of(node)
            if ref is not None:
                return self._expand(node, ref, stack=stack, location=location)
            return {
                key: self._child(key, value, stack=stack, location=location)
                for key, value in node.items()
            }
        if isinstance(node, list):
            return [
                self.resolve(item, stack=stack, location=f"{location}/{index}")
                for index, item in enumerate(node)
            ]
        return node

    def _child(self, key: Any, value: Any, *, stack: tuple[str, ...], location: str) -> Any:
        if key in OPAQUE_KEYS:
            # Copied rather than shared, so the caller's document and ours never
            # turn out to be the same object under two names.
            return copy.deepcopy(value)
        return self.resolve(value, stack=stack, location=f"{location}/{_escape(str(key))}")

    def _expand(
        self, node: dict[str, Any], ref: str, *, stack: tuple[str, ...], location: str
    ) -> Any:
        """Replace a ref node with what it points at, or with a stand-in."""
        siblings = {key: value for key, value in node.items() if key != "$ref"}

        if not ref.startswith("#"):
            self.diagnostics.add(
                EXTERNAL_REF,
                f"{ref!r} points outside this document. References to other files and "
                f"URLs are not followed, so anything using it will accept any value.",
                location=location,
            )
            return self._merge(PERMISSIVE_SCHEMA, siblings, stack=stack, location=location)

        pointer = ref[1:]

        if stack.count(pointer) >= CYCLE_DEPTH:
            self.diagnostics.add(
                REF_CYCLE,
                f"{ref!r} refers back to itself. The chain was cut after {CYCLE_DEPTH} "
                f"levels and the rest replaced with a plain object.",
                location=location,
            )
            return self._merge(CUT_SCHEMA, siblings, stack=stack, location=location)

        if self._expansions >= MAX_EXPANSIONS:
            self.diagnostics.add(
                REF_BUDGET,
                f"The document expands to more than {MAX_EXPANSIONS} inlined references, "
                f"which only deeply recursive schemas do. The rest were replaced with "
                f"plain objects.",
                location=location,
            )
            return self._merge(CUT_SCHEMA, siblings, stack=stack, location=location)

        self._expansions += 1
        target = _dereference(self._root, ref, pointer, location=location)
        # The target is walked at its own address, not at the ref site: a cycle
        # warning that says ``/components/schemas/Node/properties/children`` is
        # worth reading, one that says it via forty nested copies is not.
        resolved = self.resolve(target, stack=(*stack, pointer), location=pointer)
        return self._merge(resolved, siblings, stack=stack, location=location)

    def _merge(
        self, resolved: Any, siblings: dict[str, Any], *, stack: tuple[str, ...], location: str
    ) -> Any:
        """Lay a ref node's other keys over what the ref resolved to.

        OpenAPI 3.1 lets ``summary`` and ``description`` sit beside a ``$ref``
        and override the target's; 3.0 says they are ignored. Keeping them costs
        nothing and loses nothing, so they are kept, and they win — an author
        who wrote a description at the ref site meant that one.

        Also the one place that guarantees a fresh dict, which is what keeps
        :data:`CUT_SCHEMA` and :data:`PERMISSIVE_SCHEMA` from being handed out
        for somebody downstream to modify.
        """
        extra = {
            key: self._child(key, value, stack=stack, location=location)
            for key, value in siblings.items()
        }
        if not isinstance(resolved, dict):
            # A ref to a list or a scalar. Nothing to lay them over; this is a
            # strange document rather than a broken one.
            return resolved
        return {**resolved, **extra}


def _ref_of(node: Mapping[str, Any]) -> str | None:
    """The ref this node *is*, or ``None`` if it is an ordinary object.

    The value has to be a string. ``properties: {"$ref": {"type": "string"}}``
    is a property whose name happens to be ``$ref``, which is legal and which a
    less careful check would replace with whatever it thought was being pointed
    at.
    """
    ref = node.get("$ref")
    return ref if isinstance(ref, str) else None


def _dereference(root: dict[str, Any], ref: str, pointer: str, *, location: str) -> Any:
    """Walk a JSON pointer into the document, or say it is not there."""
    if pointer == "":
        return root  # A bare "#" is the whole document.
    if not pointer.startswith("/"):
        # "#components/schemas/Pet" — a common typo, and not a pointer.
        raise UnresolvedRefError(ref, location=location)

    current: Any = root
    for token in pointer[1:].split("/"):
        key = _unescape(token)
        if isinstance(current, dict):
            if key not in current:
                raise UnresolvedRefError(ref, location=location)
            current = current[key]
        elif isinstance(current, list):
            if not key.isdigit() or int(key) >= len(current):
                raise UnresolvedRefError(ref, location=location)
            current = current[int(key)]
        else:
            raise UnresolvedRefError(ref, location=location)
    return current


def _escape(token: str) -> str:
    """A key as it appears inside a JSON pointer (RFC 6901)."""
    return token.replace("~", "~0").replace("/", "~1")


def _unescape(token: str) -> str:
    return token.replace("~1", "/").replace("~0", "~")


__all__ = [
    "CUT_SCHEMA",
    "CYCLE_DEPTH",
    "EXTERNAL_REF",
    "MAX_EXPANSIONS",
    "OPAQUE_KEYS",
    "PERMISSIVE_SCHEMA",
    "REF_BUDGET",
    "REF_CYCLE",
    "ResolvedDocument",
    "resolve_refs",
]
