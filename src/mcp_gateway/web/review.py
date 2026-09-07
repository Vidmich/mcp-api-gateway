"""Reviewing what a refresh found (spec §5.4, task 026).

Task 025 made the gateway able to notice that an upstream has changed without
touching what it exposes. This is the other half: the small set of decisions
that turn "something happened here" back into "somebody has looked at this".

**Acknowledging is what clears Needs Attention — never a refresh.** That is the
one rule the whole flow exists to keep. A refresh runs unattended, so if a
refresh could clear the flag the flag would mean nothing: it would go up and
down between two glances at the page and the operator would never learn that an
endpoint had appeared. So a refresh only ever raises it, and only a decision
made here ever lowers it.

**A decision is per row, and the flag follows from them.** Every ``new`` and
``changed`` row offers the two or three things spec §5.4 says can be done with
it, and settling the last one clears the flag by itself — a page with nothing
left to review that still wore the badge would be telling the operator to look
at something that is not there. :func:`acknowledge` is the same act done to
every row at once, for the server that grew forty endpoints in one release.

**What each decision does is stated in one place.** ``Add`` ticks the operation
*and* settles it; ``Dismiss`` unticks it and settles it; ``Acknowledge`` settles
it and touches nothing else, which is what a ``changed`` row wants — its schema
moved, the operator has seen that, and neither its name nor its selection is
part of the news. The button labels, the sentences they produce and the writes
they make are three views of the same table below, so a button whose label
promises something the write does not do is a thing that has to be typed twice.

**Deleting is only for what the upstream dropped.** A ``removed`` row is kept
rather than deleted so that a rename survives an endpoint that disappears for an
afternoon (:func:`~mcp_gateway.db.repo.upsert_operations`); deleting it is the
operator saying the endpoint is not coming back. Doing the same to a live
operation would throw away its overrides and gain nothing — the next refresh
would insert it again, unselected and unnamed — so that is refused rather than
allowed with a warning.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Final

from sqlalchemy.ext.asyncio import AsyncSession

from mcp_gateway.db import repo

logger = logging.getLogger(__name__)

#: What a review button posts under.
DECISION_FIELD: Final = "decision"

ADD: Final = "add"
DISMISS: Final = "dismiss"
ACKNOWLEDGE: Final = "acknowledge"


@dataclass(frozen=True, slots=True)
class Decision:
    """One thing that can be decided about one unreviewed operation."""

    value: str
    label: str
    #: What the button promises, as its ``title``. Short enough to read while
    #: hovering and specific enough to be worth reading.
    hint: str


ADD_OPERATION: Final = Decision(
    ADD, "Add", "Expose this operation as a tool, and mark it reviewed."
)
DISMISS_OPERATION: Final = Decision(
    DISMISS, "Dismiss", "Mark it reviewed and leave it unexposed. It stays stored."
)
ACKNOWLEDGE_OPERATION: Final = Decision(
    ACKNOWLEDGE, "Acknowledge", "Mark the change as seen. Nothing about the tool changes."
)

#: What each status may be answered with. A status not here — ``active``, and
#: ``removed``, which is deleted rather than decided — offers nothing, which is
#: also what stops a stale page from settling a row nobody has reviewed.
DECISIONS: Final[dict[str, tuple[Decision, ...]]] = {
    "new": (ADD_OPERATION, DISMISS_OPERATION),
    "changed": (ACKNOWLEDGE_OPERATION, DISMISS_OPERATION),
}


def decisions_for(status: str) -> tuple[Decision, ...]:
    """The buttons one row offers, given what a refresh made of it."""
    return DECISIONS.get(status, ())


# --------------------------------------------------------------------------- #
# What the operator is told
# --------------------------------------------------------------------------- #

ADDED: Final = "{op_key} is now exposed as {name}."
DISMISSED: Final = "{op_key} was dismissed. It stays stored, and no client sees it."
ACKNOWLEDGED: Final = "{op_key} was marked reviewed."
DELETED: Final = "{op_key} was deleted, and the name {name} is free again."

REVIEWED: Final = "{name} was marked reviewed."
#: Added to whichever of the above cleared the badge, because a badge that
#: disappears without a word looks like something that went wrong.
NOTHING_LEFT: Final = " Nothing is left to review."

#: A decision that does not belong to the row it was made about. Almost always a
#: page left open while somebody else reviewed the same server.
NOT_A_DECISION: Final = (
    "That is not a decision this operation is waiting for. Reload the page to see it as it is."
)

#: Refused rather than allowed, for the reason in the module docstring.
STILL_IN_THE_SPEC: Final = (
    "Only an operation the upstream has dropped can be deleted. This one is still in the spec — "
    "untick it instead to stop exposing it."
)


class ReviewRefused(Exception):  # noqa: N818 - the noun is the answer, not the failure
    """A decision the row it names cannot be answered with."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


@dataclass(frozen=True, slots=True)
class Reviewed:
    """What one review did, and whether it was the last one outstanding."""

    message: str
    #: Whether **Needs Attention** came off as a result. Reported so the page
    #: can say so rather than leaving the operator to notice a missing badge.
    cleared: bool = False

    @property
    def flash(self) -> str:
        """The sentence the next page shows."""
        return f"{self.message}{NOTHING_LEFT}" if self.cleared else self.message


# --------------------------------------------------------------------------- #
# The decisions themselves
# --------------------------------------------------------------------------- #


async def review_operation(
    session: AsyncSession, server_id: int, operation_id: int, decision: str
) -> Reviewed:
    """Settle one ``new`` or ``changed`` operation the way the operator said.

    The decision is checked against the row's own status rather than against a
    list of legal values, so a button rendered before somebody else reviewed the
    same server is refused instead of applied to a row that has moved on.
    """
    operation = await repo.get_operation(session, operation_id)
    if operation is None or operation.server_id != server_id:
        raise repo.OperationNotFound(operation_id)
    if decision not in {offered.value for offered in decisions_for(operation.status)}:
        raise ReviewRefused(NOT_A_DECISION)

    op_key, name = operation.op_key, operation.effective_tool_name
    await repo.settle_operation(
        session,
        operation_id,
        selected=True if decision == ADD else False if decision == DISMISS else None,
    )
    logger.info("Operation %r of server %d was reviewed: %s", op_key, server_id, decision)

    said = {
        ADD: ADDED.format(op_key=op_key, name=name),
        DISMISS: DISMISSED.format(op_key=op_key),
        ACKNOWLEDGE: ACKNOWLEDGED.format(op_key=op_key),
    }[decision]
    return Reviewed(said, cleared=await settle_attention(session, server_id))


async def drop_operation(session: AsyncSession, server_id: int, operation_id: int) -> Reviewed:
    """Delete an operation the upstream no longer has, freeing its tool name.

    Only a ``removed`` row: see the module docstring on why deleting a live one
    is refused rather than done.
    """
    operation = await repo.get_operation(session, operation_id)
    if operation is None or operation.server_id != server_id:
        raise repo.OperationNotFound(operation_id)
    if operation.status != "removed":
        raise ReviewRefused(STILL_IN_THE_SPEC)

    op_key, name = operation.op_key, operation.effective_tool_name
    await repo.delete_operation(session, operation_id)
    logger.info("Deleted operation %r of server %d", op_key, server_id)
    return Reviewed(
        DELETED.format(op_key=op_key, name=name),
        cleared=await settle_attention(session, server_id),
    )


async def acknowledge(session: AsyncSession, server_id: int) -> Reviewed:
    """Settle every unreviewed operation of one server, and clear the flag.

    The one button for a server whose upstream shipped a release. ``removed``
    rows are left as they are, since deleting them is a separate decision and
    this one is only "I have seen all of this".
    """
    server = await repo.acknowledge_server(session, server_id)
    logger.info("Server %r was marked reviewed", server.name)
    # Not necessarily cleared: a server the gateway disabled keeps the badge
    # until somebody turns it back on, and the note would be a lie (task 100).
    return Reviewed(REVIEWED.format(name=server.name), cleared=not server.needs_attention)


async def settle_attention(session: AsyncSession, server_id: int) -> bool:
    """Take the flag off if nothing is waiting to be reviewed; say whether it went.

    Run after every per-row decision, which is what makes reviewing a server one
    row at a time end in the same place as pressing the one button.
    """
    server = await repo.require_server(session, server_id)
    if not server.needs_attention or await repo.count_unreviewed(session, server_id):
        return False
    if (await repo.acknowledge_server(session, server_id)).needs_attention:
        # Nothing is left to review, but the gateway is holding the flag up for
        # a server that stopped answering, and only the toggle answers that.
        return False
    logger.info("Server %r has nothing left to review", server.name)
    return True


__all__ = [
    "ACKNOWLEDGE",
    "ACKNOWLEDGED",
    "ACKNOWLEDGE_OPERATION",
    "ADD",
    "ADDED",
    "ADD_OPERATION",
    "DECISIONS",
    "DECISION_FIELD",
    "DELETED",
    "DISMISS",
    "DISMISSED",
    "DISMISS_OPERATION",
    "NOTHING_LEFT",
    "NOT_A_DECISION",
    "REVIEWED",
    "STILL_IN_THE_SPEC",
    "Decision",
    "ReviewRefused",
    "Reviewed",
    "acknowledge",
    "decisions_for",
    "drop_operation",
    "review_operation",
    "settle_attention",
]
