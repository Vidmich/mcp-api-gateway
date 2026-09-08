"""The spellings Python has replaced, kept out of the source tree (task 108).

``typing.TypeAlias`` was deprecated in 3.12 in favour of the ``type`` statement,
and nothing in this project's toolchain will ever say so. Ruff's ``UP040`` is the
rule that rewrites it, and that rule only fires when ``target-version`` is
``py312`` or later; the floor here is 3.11, because that is what ``README.md``,
``docs/install.md`` and SPEC §9 promise an operator. So the deprecation is real,
the fix is available, and the linter is silent about it for a reason that has
nothing to do with the code.

This module stands in for the rule until the floor moves, and is written to
retire itself on the day it does — a ban that outlives its reason is worse than
the thing it banned.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path
from typing import Final

ROOT: Final = Path(__file__).resolve().parents[2]
SRC: Final = ROOT / "src"
PYPROJECT: Final = ROOT / "pyproject.toml"

#: The version at which ``type X = ...`` exists and this file's job passes to ruff.
PEP_695: Final = (3, 12)


def python_floor() -> tuple[int, int]:
    """The lowest Python ``pyproject.toml`` says this project runs on."""
    requires = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["project"]["requires-python"]
    found = re.search(r">=\s*(\d+)\.(\d+)", str(requires))
    assert found is not None, f"no lower bound in requires-python = {requires!r}"
    return int(found[1]), int(found[2])


def test_no_module_reaches_for_the_deprecated_type_alias() -> None:
    offenders = sorted(
        path.relative_to(ROOT).as_posix()
        for path in SRC.rglob("*.py")
        if "TypeAlias" in path.read_text(encoding="utf-8")
    )

    assert not offenders, (
        f"typing.TypeAlias is deprecated since 3.12 and is back in: {offenders}. "
        "At the 3.11 floor the replacement is a bare assignment such as "
        "`Origin = tuple[str, str, int]`, which mypy still reads as a type alias. "
        "Above 3.11 it is `type Origin = tuple[str, str, int]`."
    )


def test_this_module_is_told_to_go_when_the_floor_reaches_the_type_statement() -> None:
    # The test above exists only because ruff cannot see the problem at py311.
    # When that stops being true the linter should take the job back, rather than
    # this being kept on as a second opinion nobody remembers the reason for.
    assert python_floor() < PEP_695, (
        "requires-python has reached 3.12, so `type X = ...` is available: set ruff's "
        "target-version to py312, let UP040 own this, and delete this module."
    )
