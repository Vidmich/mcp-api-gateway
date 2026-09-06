# Task 016 — Tools call proxy

**Milestone:** 4 · MCP
**Depends on:** 015
**Spec:** §6

## Goal

Execute a tool call as a real HTTP request against the upstream API and return a useful result.

## Scope

- Resolve the tool name to an operation; an unknown or newly-disabled name returns an MCP error, never an unhandled exception.
- Validate arguments against the stored `inputSchema` with `jsonschema`; on failure return `isError: true` carrying the validation message so the model can correct itself.
- Build the request: URL-encoded path substitution, query parameters, header parameters, body serialised for the operation's media type, then the server's credentials applied last.
- Shared `httpx.AsyncClient` with `http.timeout_seconds`; responses over `http.max_response_bytes` are truncated with a note appended to the result.
- Format the response: pretty-printed JSON, plain text as-is, other content types described rather than dumped.
- `4xx` / `5xx` return `isError: true` with the status line and the body — the upstream's error text is usually what the model needs.
- Leave a metrics hook at the call boundary for task 028.

## Out of scope

- Retries, circuit breaking, or rate limiting.
- Streaming responses.

## Acceptance

- [ ] respx tests assert the exact outbound method, URL, query, headers, and body for a representative operation.
- [ ] Missing a required argument returns `isError` with a readable message and makes no HTTP call.
- [ ] A 500 from the upstream returns `isError` including the upstream body.
- [ ] A timeout returns `isError` rather than propagating an exception into the session.
- [ ] An oversize response is truncated and the result says so.
- [ ] Credentials never appear in an error message or log line.
