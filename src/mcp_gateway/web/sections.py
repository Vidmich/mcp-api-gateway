"""The two sections a server can be listed in, and the words each one uses.

Spec §7.1, task 133. **API Servers** is the page an operator has had since
task 020, and **MCP Servers** is what it is now distinguished from: the same
table shape, the same actions on the same kinds of route, the same detail page
underneath, and different words wherever the thing behind the row is a
different thing — a *spec URL* is downloaded and a *tool list* is connected to,
a *base URL* is where calls go and an *endpoint* is where everything goes.

**One list per kind, not one list with a column.** The alternative was a
*Kind* column on the existing page, and it was rejected because the two kinds
are registered differently, refreshed from different things and described in
different words; a merged table would either print two vocabularies in one
column or flatten both into a vaguer one. The tool list on ``/mcp`` is where
the two kinds meet, and it is merged there.

**A section is read off the row, never off the URL.** Every path a page
renders — the name's link, the toggle's action, where a save redirects — comes
from :func:`section_of` applied to the row's ``kind``, so a server reached
under the other section's path is shown under its own and the navigation
lights the right item. What the URL a request arrived on decides is which
routes exist under it, and nothing about the answer.

**The words are fields, not a second template.** Every page and every partial
is one file with the section's words interpolated into it; the few places
where the two kinds need different *structure* — the columns of the operation
tables, the rows of the settings card — are ``{% if %}`` on the same flag.
Search-and-replace was not the tool for finding them: *spec* is right on one
page and wrong on the other, and the job was to find which. The API section's
words are exactly what the pages said before this module existed.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from mcp_gateway.db.repo import KIND_GATEWAY, KIND_MCP, KIND_OPENAPI
from mcp_gateway.web.auth import UI_PREFIX
from mcp_gateway.web.wizard import BASE_URL_SCHEME

#: The tooltip on a row with no reading behind it. Rare, now that registering
#: a server stamps the read it did (task 103): a row gets here by predating
#: that, or by being the built-in server, which has no document at all.
NEVER_DOWNLOADED: Final = "This server's spec has never been downloaded."
NEVER_LISTED: Final = "This server's tools have never been listed."

#: What the operator is told when the token in the URL names nothing any more.
PREVIEW_GONE: Final = (
    "That preview is no longer held. Fetch the spec again to carry on adding the server."
)
PREVIEW_GONE_MCP: Final = (
    "That preview is no longer held. Connect again to carry on adding the server."
)

#: What stands in for the detail page's rows when the server has none.
NO_OPERATIONS: Final = "This server has no stored tools. Refresh it to read its spec again."
NO_TOOLS: Final = "This server has no stored tools. Refresh it to list its tools again."

#: The settings form's URL box, refused for being empty. The scheme message
#: is the wizard's, since the two forms hold the same box. The API section's
#: wording is what :mod:`mcp_gateway.web.detail` has always said; the MCP
#: section's names the thing the box holds.
BASE_URL_REQUIRED: Final = (
    "A base URL is needed. It is where every tool call this server exposes goes."
)
ENDPOINT_REQUIRED: Final = "An endpoint is needed. It is where the gateway connects to this server."
ENDPOINT_SCHEME: Final = "The endpoint has to start with http:// or https://."

#: What the empty list says.
NO_SERVERS: Final = "No servers yet"
NO_API_SERVERS_MESSAGE: Final = (
    "Register an OpenAPI or Swagger service and its endpoints become MCP tools."
)
NO_MCP_SERVERS_MESSAGE: Final = (
    "Register a server that speaks MCP and its tools are published here beside the rest."
)


@dataclass(frozen=True, slots=True)
class Section:
    """One of the two lists, with its address and its vocabulary.

    The paths are methods rather than constants so that a row and its routes
    cannot disagree: every route under a section is registered from the same
    method the page's links are rendered from (:mod:`mcp_gateway.web.routes_ui`).
    """

    #: ``openapi`` or ``mcp``: the kind of row this section registers.
    kind: str
    #: The rows it lists — the API section also lists the gateway's own
    #: server, whose tools run in process and belong beside the APIs.
    kinds: tuple[str, ...]
    #: The last segment of the section's path, under :data:`UI_PREFIX`.
    slug: str
    #: What the masthead and the page heading call it.
    label: str
    #: The **Add** button, the wizard's title and its outline heading.
    add_label: str
    #: What the list's second column is headed, and what the detail page's
    #: summary and settings card call the same value.
    url_heading: str
    #: What the list's fourth column and the detail page's summary call the
    #: last reading of the upstream.
    read_heading: str
    #: The button that reads the upstream again, on both pages that offer it.
    refresh_label: str
    #: The tooltip on a row that has never been read.
    never_read: str
    #: What the list says when it is empty.
    empty_message: str
    #: What the wizard says when the token in its URL names nothing any more.
    preview_gone: str
    #: What the detail page's table says when the server has no rows at all.
    no_operations: str
    #: The settings form's URL box, refused for being empty and for its scheme.
    url_required: str
    url_scheme: str
    #: The hint under that box.
    url_hint: str

    @property
    def mcp(self) -> bool:
        """Whether this is the section that lists MCP servers.

        The one flag the templates branch on where the two kinds need different
        structure rather than different words.
        """
        return self.kind == KIND_MCP

    @property
    def path(self) -> str:
        """The list."""
        return f"{UI_PREFIX}/{self.slug}"

    @property
    def new_path(self) -> str:
        """Step 1 of the wizard: the form, and the submission that previews it."""
        return f"{self.path}/new"

    def preview_path(self, token: str) -> str:
        """Step 2's page, named by the token that holds the preview."""
        return f"{self.new_path}/{token}"

    def picker_path(self, token: str) -> str:
        """The picker's own table, re-rendered as the operator filters and ticks."""
        return f"{self.preview_path(token)}/operations"

    def back_path(self, token: str) -> str:
        """Step 1 again, rendered from the form the preview is holding (task 115)."""
        return f"{self.new_path}?from={token}"

    def detail_path(self, server_id: int | str) -> str:
        """One registered server, and everything about it that can be changed.

        Takes a string as well as an id so that the router can ask for the
        pattern — ``"{server_id}"`` — from the same method a page asks for a
        row's link, which is what keeps the two from disagreeing.
        """
        return f"{self.path}/{server_id}"

    def toggle_path(self, server_id: int | str) -> str:
        return f"{self.detail_path(server_id)}/enabled"

    def refresh_path(self, server_id: int | str) -> str:
        return f"{self.detail_path(server_id)}/refresh"

    def operations_path(self, server_id: int | str) -> str:
        return f"{self.detail_path(server_id)}/operations"

    def prefix_path(self, server_id: int | str) -> str:
        return f"{self.detail_path(server_id)}/prefix"

    def acknowledge_path(self, server_id: int | str) -> str:
        return f"{self.detail_path(server_id)}/acknowledge"

    def lists(self, kind: str) -> bool:
        """Whether a row of ``kind`` belongs on this section's page."""
        return kind in self.kinds


#: The page an operator has today, under the name task 103 gave it so that the
#: word *server* would mean the thing behind the gateway. Its words are the
#: ones every page said before there was a second section.
API: Final = Section(
    kind=KIND_OPENAPI,
    kinds=(KIND_OPENAPI, KIND_GATEWAY),
    slug="servers",
    label="API Servers",
    add_label="Add a server",
    url_heading="Base URL",
    read_heading="Last spec download",
    refresh_label="Refresh Spec",
    never_read=NEVER_DOWNLOADED,
    empty_message=NO_API_SERVERS_MESSAGE,
    preview_gone=PREVIEW_GONE,
    no_operations=NO_OPERATIONS,
    url_required=BASE_URL_REQUIRED,
    url_scheme=BASE_URL_SCHEME,
    url_hint="Where every tool call this server exposes goes.",
)

#: The addition (task 133). An endpoint is one thing with one credential, so
#: the wizard behind this section asks fewer questions and the settings card
#: shows fewer rows; everything else is the same page with the words changed.
MCP: Final = Section(
    kind=KIND_MCP,
    kinds=(KIND_MCP,),
    slug="mcp-servers",
    label="MCP Servers",
    add_label="Add an MCP server",
    url_heading="Endpoint",
    read_heading="Last tool list",
    refresh_label="Refresh tools",
    never_read=NEVER_LISTED,
    empty_message=NO_MCP_SERVERS_MESSAGE,
    preview_gone=PREVIEW_GONE_MCP,
    no_operations=NO_TOOLS,
    url_required=ENDPOINT_REQUIRED,
    url_scheme=ENDPOINT_SCHEME,
    url_hint="Where the gateway connects to list this server's tools and to call them.",
)

#: In the order the masthead lists them.
SECTIONS: Final = (API, MCP)


def section_of(kind: str) -> Section:
    """The section a row of ``kind`` belongs to.

    The gateway's own server is an API server for this purpose: it is listed
    beside them, and every word the API section uses is right for a row with
    no document at all, since those words are inside ``{% if builtin %}``
    already (task 102).
    """
    return MCP if kind == KIND_MCP else API


__all__ = [
    "API",
    "BASE_URL_REQUIRED",
    "ENDPOINT_REQUIRED",
    "ENDPOINT_SCHEME",
    "MCP",
    "NEVER_DOWNLOADED",
    "NEVER_LISTED",
    "NO_API_SERVERS_MESSAGE",
    "NO_MCP_SERVERS_MESSAGE",
    "NO_OPERATIONS",
    "NO_SERVERS",
    "NO_TOOLS",
    "PREVIEW_GONE",
    "PREVIEW_GONE_MCP",
    "SECTIONS",
    "Section",
    "section_of",
]
