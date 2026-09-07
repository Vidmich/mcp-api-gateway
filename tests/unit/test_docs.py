"""The documentation, checked against the code it describes (task 032).

Prose rots quietly. A default changes, a flag is renamed, a section is dropped,
and the page that told an operator what to write goes on saying the old thing
until somebody follows it and finds out. These tests hold the parts of the docs
that are *checkable* against the thing they document:

* every setting the loader accepts is on the configuration page, under the name
  the loader knows it by, with the environment variable that sets it;
* the page invents nothing — every ``MCP_GATEWAY_*`` name in the docs is real;
* every TOML example parses, names real keys, and shows real defaults, because
  an example is the part people copy;
* every relative link between the documents, and every heading anchor one of
  them points at, resolves.

What they cannot check is whether the prose is *true* — that the systemd unit
works, that the quickstart is followable. The service recipes carry their own
"verified" or "untested" marker for that, and the last test here only insists
that each of them says which it is.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path
from typing import Any

import pytest

from mcp_gateway.config import CLI_TO_SETTING, ENV_PREFIX, NESTING_SEPARATOR, SECTION_MODELS

ROOT = Path(__file__).resolve().parents[2]
DOCS = ROOT / "docs"
README = ROOT / "README.md"

#: The four pages spec §9 names, plus the one that links to them.
PAGES = (
    README,
    DOCS / "install.md",
    DOCS / "configuration.md",
    DOCS / "service-setup.md",
    DOCS / "security.md",
)

CONFIGURATION = DOCS / "configuration.md"
SERVICE_SETUP = DOCS / "service-setup.md"
SECURITY = DOCS / "security.md"

#: ``MCP_GATEWAY_<SECTION>__<KEY>``. A section is one word; a key may hold
#: underscores, which is why the two halves are matched separately.
ENV_NAME = re.compile(rf"{ENV_PREFIX}([A-Z0-9]+){NESTING_SEPARATOR}([A-Z0-9_]+)")

#: A markdown link with a relative target: ``[text](path)`` or ``[text](path#anchor)``.
LINK = re.compile(r"\[[^\]]*\]\(([^)\s]+)\)")

FENCED_TOML = re.compile(r"```toml\n(.*?)```", re.DOTALL)

HEADING = re.compile(r"^#{1,6}\s+(.*?)\s*$", re.MULTILINE)

#: Values shown in an example because they *illustrate* rather than because they
#: are the default. Everything else in a TOML block has to match the model, which
#: is what stops a changed default from leaving a stale number on the page.
ILLUSTRATIVE = frozenset(
    {
        ("admin", "username"),
        ("admin", "password"),
        ("admin", "password_hash"),
        ("mcp", "auth_token"),
        # Carries the running version, so it cannot be pinned here. Checked for
        # its shape instead, below.
        ("http", "user_agent"),
    }
)


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def settings_keys() -> list[tuple[str, str]]:
    """Every ``(section, key)`` the loader accepts."""
    return [
        (section, key) for section, model in SECTION_MODELS.items() for key in model.model_fields
    ]


def default_of(section: str, key: str) -> Any:
    return SECTION_MODELS[section].model_fields[key].default


def slug(heading: str) -> str:
    """GitHub's anchor for a heading: lowercased, punctuation dropped."""
    text = heading.replace("`", "").strip().lower()
    text = re.sub(r"[^\w\s-]", "", text)
    return re.sub(r"\s+", "-", text)


def anchors(path: Path) -> set[str]:
    return {slug(heading) for heading in HEADING.findall(read(path))}


# --------------------------------------------------------------------------- #
# The pages exist and hang together
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("page", PAGES, ids=lambda page: page.name)
def test_every_page_the_project_promises_exists_and_says_something(page: Path) -> None:
    assert page.is_file(), f"{page} is missing"
    assert len(read(page)) > 500


def test_the_readme_links_to_each_of_the_four_documents() -> None:
    body = read(README)
    for page in PAGES[1:]:
        assert f"docs/{page.name}" in body, f"the README never points at {page.name}"


@pytest.mark.parametrize("page", PAGES, ids=lambda page: page.name)
def test_every_relative_link_resolves(page: Path) -> None:
    """A link to a file that is not there, or to an anchor that is not on it."""
    for target in LINK.findall(read(page)):
        if target.startswith(("http://", "https://", "mailto:")):
            continue
        path_part, _, fragment = target.partition("#")
        destination = page if not path_part else (page.parent / path_part).resolve()
        assert destination.is_file(), f"{page.name} links to {target}, which is not a file"
        if fragment:
            assert fragment in anchors(destination), (
                f"{page.name} links to {target}, and {destination.name} has no such heading"
            )


# --------------------------------------------------------------------------- #
# Every setting, under the name the loader knows it by
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(("section", "key"), settings_keys(), ids=lambda part: str(part))
def test_every_setting_is_on_the_configuration_page(section: str, key: str) -> None:
    """Spec §3.2's keys, and any the code has grown since."""
    assert f"{section}.{key}" in read(CONFIGURATION)


@pytest.mark.parametrize(("section", "key"), settings_keys(), ids=lambda part: str(part))
def test_every_setting_has_its_environment_variable_written_out(section: str, key: str) -> None:
    """Nobody should have to derive the name from the rule to be sure of it."""
    name = f"{ENV_PREFIX}{section.upper()}{NESTING_SEPARATOR}{key.upper()}"
    assert name in read(CONFIGURATION)


@pytest.mark.parametrize("flag", sorted({f"--{dest.replace('_', '-')}" for dest in CLI_TO_SETTING}))
def test_every_flag_that_sets_something_is_documented(flag: str) -> None:
    assert flag in read(CONFIGURATION)


def test_the_two_flags_that_set_nothing_are_documented_too() -> None:
    """``--config`` and ``--version`` set no key, so the table cannot carry them."""
    body = read(CONFIGURATION)
    assert "--config" in body
    assert "--version" in body


@pytest.mark.parametrize("page", PAGES, ids=lambda page: page.name)
def test_the_docs_invent_no_settings(page: Path) -> None:
    """Every ``MCP_GATEWAY_*`` name in the prose is one the loader would read."""
    for section, key in ENV_NAME.findall(read(page)):
        model = SECTION_MODELS.get(section.lower())
        assert model is not None, f"{page.name} names section [{section.lower()}], which is not one"
        assert key.lower() in model.model_fields, (
            f"{page.name} names {section.lower()}.{key.lower()}, which is not a setting"
        )


# --------------------------------------------------------------------------- #
# The examples, which are the part people copy
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("page", PAGES, ids=lambda page: page.name)
def test_every_toml_example_parses_and_names_real_keys(page: Path) -> None:
    for block in FENCED_TOML.findall(read(page)):
        parsed = tomllib.loads(block)
        for section, values in parsed.items():
            model = SECTION_MODELS.get(section)
            assert model is not None, f"{page.name} shows [{section}], which is not a section"
            for key in values:
                assert key in model.model_fields, (
                    f"{page.name} shows {section}.{key}, which is not a setting"
                )


def test_the_examples_show_the_defaults_that_are_actually_the_defaults() -> None:
    """A value in an example is either the real default or deliberately not one."""
    for block in FENCED_TOML.findall(read(CONFIGURATION)):
        for section, values in tomllib.loads(block).items():
            for key, shown in values.items():
                if (section, key) in ILLUSTRATIVE:
                    continue
                default = default_of(section, key)
                if isinstance(default, Path):
                    shown = Path(shown)
                assert shown == default, (
                    f"configuration.md shows {section}.{key} = {shown!r}, "
                    f"but the default is {default!r}"
                )


def test_the_user_agent_example_still_looks_like_a_user_agent() -> None:
    """It carries the version, so only its shape can be pinned."""
    shown = [
        values["user_agent"]
        for block in FENCED_TOML.findall(read(CONFIGURATION))
        for name, values in tomllib.loads(block).items()
        if name == "http" and "user_agent" in values
    ]
    assert shown, "the [http] example no longer shows a user agent"
    for value in shown:
        assert value.startswith("mcp-gateway/")


# --------------------------------------------------------------------------- #
# The two pages that exist to be honest
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "gap",
    [
        # Spec §2's deliberate v1 gaps, each named on the page rather than implied.
        "SSRF",
        "keys.json",
        "auth_token",
        "TLS",
    ],
)
def test_each_deliberate_gap_is_stated_on_the_security_page(gap: str) -> None:
    assert gap in read(SECURITY)


def test_the_readme_carries_the_warning_rather_than_only_linking_to_it() -> None:
    """A reader who never opens docs/ still has to meet this."""
    body = read(README)
    assert "SSRF" in body
    assert "docs/security.md" in body


@pytest.mark.parametrize(
    "recipe", ["systemd", "launchd", "NSSM", "Task Scheduler", "Docker", "nginx"]
)
def test_every_service_recipe_says_whether_it_was_run(recipe: str) -> None:
    """Task 032: each recipe is run on its platform, or is marked untested."""
    rows = [line for line in read(SERVICE_SETUP).splitlines() if line.startswith("| ")]
    matching = [row for row in rows if recipe in row]
    assert matching, f"{recipe} is not in the tested/untested table"
    for row in matching:
        assert "**Verified" in row or "**Untested" in row, f"the {recipe} row claims neither: {row}"
