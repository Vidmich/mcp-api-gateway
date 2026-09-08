# Task 108 — The annotations the language has moved on from

**Milestone:** 13 · Housekeeping (post-v1)
**Depends on:** 001, 006, 008
**Spec:** §9, §10

## Goal

Nothing here warns. The test suite runs with `filterwarnings = ["error", ...]`, so an annotation
that raised a `DeprecationWarning` at import time would already be a red test rather than a task.
What is left is the quiet kind: spellings the language has replaced and still accepts without a
word, which stay in a codebase until somebody goes looking.

There is exactly one, `typing.TypeAlias`, deprecated in 3.12 in favour of the `type` statement, in
five places across three modules:

| Module | Alias |
|---|---|
| `crypto.py` | `Secret`, `Credential` |
| `openapi/fetch.py` | `ParsedAs`, `_Hop` |
| `outbound.py` | `Origin` |

The toolchain is silent about all five and will stay silent. Ruff's `UP040` is the rule that would
rewrite them, and it only fires when `target-version` is `py312` or later; this project pins
`py311`, because 3.11 is the supported floor and `type` arrived in 3.12. So the deprecation is real,
the fix is available, and nothing in CI will ever mention it — which is the reason to write it down
rather than wait for a linter to.

## Scope

- **The five aliases become bare assignments.** `Origin = tuple[str, str, int]`, and so on. This is
  the answer that works at the floor the project actually supports; mypy reads an unannotated
  assignment of a type expression as an implicit type alias, so nothing is lost from the checking.

  What is lost is the label — `TypeAlias` existed to tell a reader "this line is a type, not a
  value" — and it is worth saying why that is acceptable here rather than pretending it is free. All
  five already carry a `#:` comment above them saying what the alias is for, in a codebase where
  that is the convention; the annotation was restating, in a keyword, something the sentence above
  it says better.

- **Two of the five are load-bearing at runtime, not just to the checker.** `Secret` and
  `Credential` are pydantic `Annotated` aliases: `Credential` is fed to `TypeAdapter` and is what
  discriminates a stored payload by its `type` field. Changing how they are spelled changes an
  expression that is evaluated, so the acceptance below asks for a credential round-tripped through
  encrypt and decrypt, not only for a clean `mypy`.

- **Take the imports out with them.** `TypeAlias` leaves the `from typing import ...` line in all
  three modules; `__all__` stays sorted, which `RUF022` enforces.

- **Do not raise the floor.** The 3.12 answer is `type Origin = tuple[str, str, int]`, and it is
  the better one — it makes the alias lazy, keeps the label, and turns `UP040` back on so the
  question stops needing a task. It costs dropping 3.11, which `pyproject.toml`, the CI matrix,
  `README.md`, `docs/install.md` and SPEC §9 all state as a promise. That is a decision about who
  can install this, not a tidy-up, and it belongs to whoever makes it. Record the replacement in a
  comment or in the notes so the day the floor moves, the change is one `sed` and not another
  audit.

- **Make it stay fixed.** Ruff cannot hold this line at `py311`, so a small test does: one scan of
  `src/**/*.py` for `TypeAlias`, in the manner of the repo-hygiene tests already in
  `test_packaging.py` and `test_docs.py`. Its failure message should name the replacement and the
  condition under which the ban should be lifted — when the floor reaches 3.12, the right move is
  to delete the test and let `UP040` do the work — so that a future reader meets a reason and not
  just a prohibition.

- **Record what the audit found clean.** The sweep is most of the work in this task and none of the
  diff, so it should survive in the notes rather than being re-derived by the next person who
  wonders. As of writing: no `typing.List` / `Dict` / `Tuple` / `Optional` / `Union` anywhere in
  `src` or `tests`; `Callable`, `AsyncIterator` and friends already come from `collections.abc`; no
  bare `datetime.utcnow()` — `db.models.utcnow` is `datetime.now(dt.UTC)`; no pydantic v1 idioms
  (`@validator`, `class Config`, `.dict()`, `parse_obj`); no FastAPI `@app.on_event`; no SQLAlchemy
  `declarative_base`; no `typing_extensions` dependency at all. The whole of `typing` in use is
  `Annotated`, `Any`, `Final`, `Literal`, `NamedTuple`, `Protocol`, `TYPE_CHECKING`, `TypeAlias`,
  `TypeVar`, `assert_never`, `cast`.

## Out of scope

- **Raising `requires-python`**, changing the CI matrix, or editing the "3.11 or newer" sentences in
  `README.md`, `docs/install.md` and SPEC §9. See the bullet above; it is a separate decision.
- **The two `TypeVar`s** in `builtin/tools.py` and `web/wizard.py`. Legacy spelling, but not
  deprecated — nothing in the docs or the interpreter says a word against them, and PEP 695's
  replacement needs 3.12 exactly as `type` does. They go in the same change as the floor, or not at
  all.
- **`from __future__ import annotations`.** Still the right call at 3.11 and not deprecated.
- **Third-party deprecations.** The starlette-through-anyio alias already has its own named entry in
  `filterwarnings` with a comment saying why; removing that entry when it stops being needed is its
  own small change, not this one.
- **Behaviour of any kind.** No route, no form field, no stored value, no serialised shape. If the
  diff touches a template or a migration, the task has gone wrong.

## Acceptance

- [x] `TypeAlias` appears nowhere in `src`, and none of the three modules still imports it.
- [x] The five aliases are bare assignments and are still used at exactly the sites they were used
      at before.
- [x] A credential of each of the four shapes still round-trips through `CredentialCipher`, and
      `_ADAPTER` still rejects a payload with an unknown `type` — the discriminated union survives
      the respelling.
- [x] `mypy src` passes at `python_version = "3.11"` under `strict`, with no new `type: ignore` and
      no new `cast`.
- [x] `ruff check .` and `ruff format --check .` pass, and every `__all__` stays sorted.
- [x] A test fails if `TypeAlias` returns to `src`, and its message names both the 3.12 replacement
      and the fact that the test should be deleted in favour of `UP040` when the floor moves.
- [x] The full suite passes with no assertion changed — this task alters no behaviour, so any test
      that needed editing is evidence something was rewritten that should not have been.
- [x] `pyproject.toml`'s `requires-python` and `target-version`, the CI matrix and every "3.11"
      sentence in the documentation are byte-for-byte unchanged.
- [x] The audit's clean findings are written into the task's Notes, so the next sweep starts from a
      list rather than from nothing.

## Notes

**Five aliases, three modules, and nothing else.** `Secret` and `Credential` in `crypto.py`,
`ParsedAs` and `_Hop` in `openapi/fetch.py`, `Origin` in `outbound.py`, each now a bare assignment
with its `#:` comment untouched above it. The diff is three lines per file — the alias and the
`from typing import ...` line it came in on — and `git diff --stat` shows no other module touched.
`ruff format` collapsed `Secret` onto one line once `: TypeAlias` stopped pushing it over 100
characters; that is the only reflow in the change.

**The checker never noticed the difference.** `mypy src` passes strict at `python_version = "3.11"`
with no new `type: ignore` and no new `cast`, which is the whole basis for the bare assignment being
an acceptable answer at this floor: mypy reads an unannotated assignment of a type expression as an
implicit alias, so the five names still check as types everywhere they were already used.

**The two runtime aliases were already covered, which is why no test was added for them.**
`Credential` is an evaluated expression, not a hint — it is what `TypeAdapter` discriminates a
stored payload with — so respelling it could have broken decryption without troubling mypy.
`test_every_payload_shape_round_trips` is parametrised over all four shapes and
`test_a_payload_shape_this_version_does_not_know_is_unreadable` feeds it an unknown `type`; both
still pass unedited. A test that has to be rewritten to keep passing proves nothing, so the right
outcome here was to change none of them.

**The guard, and why it has an expiry date.** `tests/unit/test_annotations.py` holds two tests.
The first scans `src/**/*.py` and fails if `TypeAlias` returns, naming the bare-assignment
replacement for 3.11 and the `type X = ...` one for above it. The second reads `requires-python`
and fails the moment the floor reaches 3.12, saying to set ruff's `target-version` to `py312`, let
`UP040` own the rule, and delete the module — so the ban retires itself instead of outliving the
reason it exists.

Both directions were checked rather than assumed. A throwaway `src/mcp_gateway/_guardprobe.py`
containing `X: TypeAlias = int` made the scan fail with the intended message and was removed; the
floor parser was run against `>=3.11`, `>=3.12`, `>=3.11,<4.0` and `>= 3.14` and retires on exactly
the last two.

**No SPEC amendment.** §9 is a file tree and a dependency list and §10 names categories of test,
neither of which this changes, and the acceptance requires every "3.11" sentence to stay
byte-for-byte. `pyproject.toml`, `.github/workflows/`, `README.md`, `docs/install.md` and `SPEC.md`
are all absent from the diff.

**What the audit found clean**, so the next sweep starts from a list. No `typing.List` / `Dict` /
`Tuple` / `Set` / `Type` / `Optional` / `Union` anywhere in `src` or `tests`. `Callable`,
`AsyncIterator`, `Mapping` and the rest already come from `collections.abc`. No bare
`datetime.utcnow()` — `db.models.utcnow` is `datetime.now(dt.UTC)`, and every other `utcnow()` in
the tree is a call to it. No pydantic v1 idioms: no `@validator`, no `class Config`, no `.dict()`,
no `parse_obj`. No FastAPI `@app.on_event`. No SQLAlchemy `declarative_base`. No
`typing_extensions` dependency at all. After this change the whole of `typing` in use is
`Annotated`, `Any`, `Final`, `Literal`, `NamedTuple`, `Protocol`, `TYPE_CHECKING`, `TypeVar`,
`assert_never` and `cast`.

The two `TypeVar`s in `builtin/tools.py` and `web/wizard.py` stayed, as the scope said: legacy
spelling, but nothing deprecates them, and PEP 695's replacement needs 3.12 exactly as `type` does.
They belong to whichever change moves the floor.

**Results.** `ruff check .` and `ruff format --check .` pass, `mypy src` reports no issues in 61
source files, and the full suite is 2116 passed, 2 skipped in 259.35s (0:04:19) — two more tests than before, both of them the new guard, and
no existing assertion edited.
