# Tasks

Implementation tasks for the OpenAPI → MCP Gateway, ordered by the milestones in [SPEC.md](../SPEC.md) §11.
Each file is self-contained: goal, scope, explicit non-scope, and acceptance criteria.

Dependencies are listed as a single chain, which is the safe order to work in. Tasks within the
same milestone that touch different modules can overlap in practice — 009, 010 and 011 are
independent of each other, as are 020–023 once 019 is done.

Tasks 001–104 were written while the program was called `mcp-gateway` and the distribution
`mcp-spec-gateway`, and they still say so. Task 105 renamed both to `mcp-api-gateway`; the old
names are left in the earlier files because they are a record of what was asked at the time, not
instructions to follow now.

| # | Task | Milestone |
|---|---|---|
| 001 | [Project scaffolding](001-project-scaffolding.md) | 1 · Skeleton |
| 002 | [Configuration model and precedence](002-configuration-model-and-precedence.md) | 1 · Skeleton |
| 003 | [First run bootstrap and keys](003-first-run-bootstrap-and-keys.md) | 1 · Skeleton |
| 004 | [App factory and serve](004-app-factory-and-serve.md) | 1 · Skeleton |
| 005 | [Database models and migrations](005-database-models-and-migrations.md) | 2 · Storage |
| 006 | [Credential encryption](006-credential-encryption.md) | 2 · Storage |
| 007 | [Repository layer](007-repository-layer.md) | 2 · Storage |
| 008 | [Spec fetching](008-spec-fetching.md) | 3 · Ingestion |
| 009 | [Ref resolution](009-ref-resolution.md) | 3 · Ingestion |
| 010 | [Swagger 2 conversion](010-swagger-2-conversion.md) | 3 · Ingestion |
| 011 | [Json schema normalization](011-json-schema-normalization.md) | 3 · Ingestion |
| 012 | [Operation extraction and input schema](012-operation-extraction-and-input-schema.md) | 3 · Ingestion |
| 013 | [Tool naming](013-tool-naming.md) | 3 · Ingestion |
| 014 | [Mcp endpoint wiring](014-mcp-endpoint-wiring.md) | 4 · MCP |
| 015 | [Tools list](015-tools-list.md) | 4 · MCP |
| 016 | [Tools call proxy](016-tools-call-proxy.md) | 4 · MCP |
| 017 | [Mcp bearer auth](017-mcp-bearer-auth.md) | 4 · MCP |
| 018 | [Admin authentication](018-admin-authentication.md) | 5 · Configuration UI |
| 019 | [Ui shell](019-ui-shell.md) | 5 · Configuration UI |
| 020 | [Server list page](020-server-list-page.md) | 5 · Configuration UI |
| 021 | [Add server wizard step 1](021-add-server-wizard-step-1.md) | 5 · Configuration UI |
| 022 | [Add server wizard step 2](022-add-server-wizard-step-2.md) | 5 · Configuration UI |
| 023 | [Server detail page](023-server-detail-page.md) | 5 · Configuration UI |
| 024 | [Json api](024-json-api.md) | 5 · Configuration UI |
| 025 | [Refresh diff engine](025-refresh-diff-engine.md) | 6 · Refresh |
| 026 | [Refresh ui and review](026-refresh-ui-and-review.md) | 6 · Refresh |
| 027 | [Auto refresh scheduler](027-auto-refresh-scheduler.md) | 6 · Refresh |
| 028 | [Metrics collection](028-metrics-collection.md) | 7 · Monitoring |
| 029 | [Metrics aggregation api](029-metrics-aggregation-api.md) | 7 · Monitoring |
| 030 | [Monitoring page](030-monitoring-page.md) | 7 · Monitoring |
| 031 | [Metrics retention](031-metrics-retention.md) | 7 · Monitoring |
| 032 | [Documentation](032-documentation.md) | 8 · Ship |
| 033 | [End to end test suite](033-end-to-end-test-suite.md) | 8 · Ship |
| 034 | [Release pipeline](034-release-pipeline.md) | 8 · Ship |

## Backlog

Numbered from 100 so they never collide with the milestone chain above. Each one still
names its dependencies; none of them is needed for v1 to ship.

| # | Task | Milestone |
|---|---|---|
| 100 | [Auto-disable a failing server](100-auto-disable-failing-servers.md) | 9 · Resilience |
| 101 | [Per-server rate limits](101-per-server-rate-limits.md) | 9 · Resilience |
| 102 | [The built-in gateway server](102-builtin-gateway-server.md) | 10 · Self-service |
| 103 | [The server list, in the words an operator uses](103-server-list-wording-and-actions.md) | 11 · UI polish |
| 104 | [The Configuration page](104-configuration-page.md) | 11 · UI polish |
| 105 | [One name: mcp-api-gateway](105-rename-to-mcp-api-gateway.md) | 12 · Naming |
| 106 | [The server list, at a glance](106-server-list-status-column.md) | 11 · UI polish |
| 107 | [The server page, in the same terms as the list](107-server-detail-status-and-layout.md) | 11 · UI polish |
| 108 | [The annotations the language has moved on from](108-deprecated-annotations.md) | 13 · Housekeeping |
| 109 | [The front door](109-landing-page.md) | 11 · UI polish |
| 110 | [The row that is not there yet](110-new-server-row-not-shown.md) | 11 · UI polish |
| 111 | [The identifier that identified nothing](111-remove-server-slug.md) | 13 · Housekeeping |
| 112 | [Turning a server off from its own page](112-detail-page-enable-disable.md) | 11 · UI polish |
| 113 | [Settings you read before you change](113-detail-settings-view-and-edit.md) | 11 · UI polish |
| 114 | [One Save, and a box that ticks the column](114-tools-table-one-save-and-a-header-tick.md) | 11 · UI polish |
| 115 | [What step 2 shows, and what Back gives back](115-picker-header-tick-prefixed-names-and-back.md) | 11 · UI polish |
| 116 | [What the tools table stops saying](116-tools-table-columns-and-a-printed-prefix.md) | 11 · UI polish |
| 117 | [Three checks that assumed the machine they were written on](117-release-checks-that-assumed-their-machine.md) | 8 · Ship |
| 118 | [Naming the tools before the server exists](118-naming-the-tools-before-the-server-exists.md) | 11 · UI polish |
| 119 | [The measure that leaves the settings card half empty](119-the-measure-that-left-the-settings-card-half-empty.md) | 11 · UI polish |
| 120 | [Two bars that hold nothing but a heading](120-two-bars-that-hold-nothing-but-a-heading.md) | 11 · UI polish |
| 121 | [Two narrow cards above a wide one](121-two-narrow-cards-above-a-wide-one.md) | 11 · UI polish |
| 122 | [The zero on the Sent total, and the charts Back leaves blank](122-sent-bytes-and-the-charts-back-leaves-blank.md) | 11 · UI polish |
| 123 | [Charts that do not draw until something else moves](123-charts-that-do-not-draw-until-something-else-moves.md) | 11 · UI polish |
| 124 | [Keep the canvas, swap the numbers](124-keep-the-canvas-and-swap-the-numbers.md) | 11 · UI polish |
| 125 | [Sending the numbers on](125-metrics-export-to-new-relic.md) | 14 · Observability |
| 126 | [The second door, and the page it was never on](126-mcp-token-on-the-configuration-page.md) | 15 · Access control |
| 127 | [The line that says INFO and error at once](127-the-line-that-says-info-and-error-at-once.md) | 13 · Housekeeping |
| 128 | [Sign in with what you just set](128-sign-in-with-what-you-just-set.md) | 15 · Access control |
| 129 | [The form that fell to the bottom half of the page](129-the-form-that-fell-to-the-bottom-half.md) | 11 · UI polish |
| 130 | [A second kind of upstream](130-a-second-kind-of-upstream.md) | 16 · Upstream MCP servers |
| 131 | [An MCP server's tools, as operations](131-an-mcp-servers-tools-as-operations.md) | 16 · Upstream MCP servers |
| 132 | [Calling through to an MCP server](132-calling-through-to-an-mcp-server.md) | 16 · Upstream MCP servers |
| 133 | [The MCP Servers page](133-the-mcp-servers-page.md) | 16 · Upstream MCP servers |
| 134 | [The same server from the API, from the built-in tools, and in the docs](134-the-same-server-from-the-api-and-the-docs.md) | 16 · Upstream MCP servers |

