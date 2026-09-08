# Task 111 — The identifier that identified nothing

**Milestone:** 13 · Housekeeping (post-v1)
**Depends on:** 005, 007, 023, 024, 102
**Spec:** §1, §4, §7.1, §7.3

## Goal

The server detail Settings form asks for a **Slug**. It is required, it is editable, and its hint
says *"Its identifier in URLs."* No URL in this application contains a slug. Every route is keyed
by the integer id — `/ui/servers/{server_id}`, `/api/v1/servers/{server_id}` — and neither
`routes_ui.py` nor `routes_api.py` contains the word.

What `servers.slug` actually does, in full:

| Site | What it does |
|---|---|
| `picker.py` `free_slug` | derives it from the display name at save time, `petstore`, `petstore-2` |
| `repo.py:633` `get_server_by_slug` | answers "is this taken", for `free_slug`, the detail form and the built-in seed |
| `repo.py:701` | copies it into the new row |
| `repo.py:943` `_summary_fields` | emits `"slug"` in the JSON API's server payload |
| `detail.py:416` | puts it in the form the operator is being shown |
| `models.py:203` | `__repr__` |

That is the whole list. Nothing routes by it, nothing names a tool from it, nothing looks a server
up by it. The column that *does* name tools is `tool_prefix`, which is a separate unique column
with its own box directly beneath this one — and that box says "Changing it renames all of them at
once", which is the difference. `tool_prefix` defaults to the slug **once**, at creation, and the
two have been independent ever since. So editing Slug on a saved server changes two things: the
value in that box, and a field in an API payload that no endpoint accepts back.

Remove the field, and the column behind it.

## Scope

- **The form field and everything that reads it.** The `field("slug", …)` call in
  `server_detail.html`, and in `web/detail.py` the `SLUG_FIELD`, `SLUG_REQUIRED` and `SLUG_TAKEN`
  constants, the parse that derives and truncates it, the duplicate check in the refusal path, and
  their `__all__` entries. Tool prefix keeps everything it has, including the live rename preview.

- **The column.** `Server.slug` and the `uq_servers_slug` constraint; `NewServer.slug`,
  `ServerPatch.slug`, the `"slug"` entry in `update_server`'s field tuple, `get_server_by_slug`,
  the copy in `create_server`, and the `"slug"` key in `_summary_fields`. `Server.__repr__` names
  the row by `tool_prefix` instead, which is the word an operator would recognise.

- **The migration is the hard part, and it has a trap in it.** This column cannot be dropped in
  place. `uq_servers_slug` is a *table* constraint, SQLite implements it with an internal
  auto-index, and `ALTER TABLE … DROP COLUMN` refuses a column that is indexed. So `servers` has to
  be rebuilt — and revision `0002_auto_disable`'s docstring says precisely why a batch rebuild of
  this table is dangerous: batch mode builds the new table from *reflection*, reflection does not
  report `sqlite_autoincrement`, and losing it breaks the promise SPEC §4 makes that a deleted
  server's id is never handed to its replacement, which is what keeps metrics from being attributed
  to the wrong server.

  So: `op.batch_alter_table("servers", copy_from=…, table_kwargs={"sqlite_autoincrement": True})`,
  with the table written out in the migration rather than reflected. Nothing about this table's
  shape should be discovered at upgrade time.

  `tests/integration/test_migrations.py:147` already asserts `AUTOINCREMENT` survives a migration,
  and it exists for exactly this. It must still pass, unedited.

- **Decide what `downgrade` does, and say so in the docstring.** The column is `NOT NULL UNIQUE`,
  so a downgrade cannot give back what was dropped. Either re-derive it from `name` with a slugify
  frozen into the migration — copied, not imported, for the reason revision `0005_extension` gives
  about its `_hash`: a migration describes what was done at one moment, and importing today's code
  makes yesterday's migration mean something new tomorrow — disambiguating collisions the way
  `free_slug` did; or refuse to downgrade at all. Both are defensible. An undocumented choice is
  not.

- **The JSON API loses a field, and that is a breaking change.** `_summary_fields` stops emitting
  `slug`, and `ServerUpdate` stops accepting one — with `extra="forbid"`, a caller still sending
  `"slug"` gets a `422` rather than being quietly ignored. Say so wherever this release is
  described. At `0.1.0` the recommendation is to break it cleanly rather than accept-and-ignore for
  a version; take the other road only deliberately.

- **What stays, and must not be swept up with it.**
  - `naming.server_slug()` — the slugify that proposes a *tool prefix* from a display name
    (`api.py`, and the wizard). Same function, same output, one caller fewer.
  - `FALLBACK_SLUG`, for the same reason.
  - `MAX_SLUG`, which is `tool_prefix`'s width too — but its comment reads "The width of
    `servers.slug` and `servers.tool_prefix`" and has to lose half of that.
  - The built-in server's reservation of `gateway`. `free_identity` keeps stepping aside for a
    database that already holds the word, but now checks one column instead of two — and its
    "both columns at once, because they are one word" paragraph goes with the second column.

- **SPEC, in four places.** §1's tool-naming row says `<server_slug>__<operationId>` where it means
  the prefix; §4's `servers` table has a `slug` row, and its built-in paragraph reserves "`slug` and
  `tool_prefix`"; §7.1's detail-page bullet lists "name, slug/prefix"; §7.3's `PATCH` field list
  names `slug`. Afterwards no sentence in the spec should say a server has one.

- **Tests: twenty-one files mention the word.** Most only pass `slug=` to a `NewServer` fixture and
  simply lose an argument. The ones with something to say are `test_repo.py` (the lookup),
  `test_ui_detail.py` (the form field), `test_ui_picker.py` (`free_slug`'s disambiguation),
  `test_builtin.py` (the reservation), `test_api.py` (the payload) and `test_db_models.py` (the
  constraint). A test that loses a keyword argument needs no thought; a test that asserted the slug
  *did* something has to say what it asserts now, or go.

## Out of scope

- **`tool_prefix`.** It stays unique, stays editable, keeps its live rename preview. This task
  removes the column it was confused with, not the one that works.
- **`naming.server_slug` and the slugify rules.** Unchanged, including the truncation and the
  `path_slug` / `ROOT_SLUG` machinery, which is about paths and never was about servers.
- **Introducing a slug-keyed URL.** If `/ui/servers/petstore` is wanted one day it is a feature,
  with its own answers about renames and redirects — not a reason to keep a column nothing reads.
- **The wizard.** It never asked for a slug and still will not; its Tool prefix box is untouched.
- **Server ids and `sqlite_autoincrement`.** Preserved, not revisited. The migration's whole
  difficulty is preserving them.

## Acceptance

- [ ] No Slug field renders on the detail page, for an editable server or the built-in one, and
      Tool prefix is unchanged and still previews the renames it would cause.
- [ ] Nothing in `src` names a slug column: what is left of the word is `server_slug`, `MAX_SLUG`,
      `FALLBACK_SLUG`, `ROOT_SLUG` and the path-slug naming, all of which are about names, not rows.
- [ ] A migration at head drops `servers.slug` and `uq_servers_slug`, and
      `test_an_existing_database_keeps_its_rows_across_a_migration` still finds `AUTOINCREMENT` in
      the rebuilt table, unedited.
- [ ] A database created before this revision — with servers, operations and recorded metrics —
      migrates with every row present and every server id unchanged.
- [ ] `downgrade` does what the migration's docstring says it does, and a test demonstrates it.
- [ ] `GET /api/v1/servers` and `GET /api/v1/servers/{id}` no longer carry `slug`, and a `PATCH`
      that sends one is refused rather than ignored.
- [ ] The built-in server still takes `gateway`, and still steps aside for a database that already
      had it.
- [ ] SPEC is amended in §1, §4, §7.1 and §7.3, and no sentence there says a server has a slug.
- [ ] `ruff check`, `ruff format --check` and `mypy src` pass, and the suite passes with assertions
      changed only where removing the column made them wrong.
