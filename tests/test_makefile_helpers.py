"""Unit tests for the Makefile query helpers in tests/_makefile.py.

The toolchain tests call these helpers against the repository Makefile. These
tests drive them over temporary Makefiles instead, so each parsing rule and
each failure is pinned without depending on the live Makefile's contents, and
so a query can be shown never to start a process.
"""

from __future__ import annotations

import typing as typ

import pytest

from tests import _makefile
from tests._makefile import makefile_pin, makefile_recipe, makefile_variable_block

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    import pathlib

SAMPLE = (
    "RUFF_VERSION ?= 0.16.4\n"
    "PYLINT_TOOL = PYLINTHOME=.cache \\\n"
    "\tuv tool run --from pylint==4.0.8\n"
    "\n"
    "lint: uv ## Run linters\n"
    "\t$(RUFF) check\n"
    "\t$(MAKE) lint-pylint\n"
    "\n"
)


@pytest.fixture
def makefile(tmp_path: pathlib.Path) -> pathlib.Path:
    """Return a small Makefile carrying one pin, one block and one recipe."""
    path = tmp_path / "Makefile"
    path.write_text(SAMPLE, encoding="utf-8")
    return path


def test_pin_reads_the_supplied_makefile(makefile: pathlib.Path) -> None:
    """A pin is read from the file passed, not the repository Makefile."""
    assert makefile_pin("RUFF_VERSION", makefile=makefile) == "0.16.4", (
        "the pin must come from the supplied Makefile"
    )


def test_variable_block_keeps_its_continuations(makefile: pathlib.Path) -> None:
    """A continued assignment is returned whole, backslashes included."""
    block = makefile_variable_block("PYLINT_TOOL", makefile=makefile)

    assert block == (
        "PYLINT_TOOL = PYLINTHOME=.cache \\\n\tuv tool run --from pylint==4.0.8"
    ), f"the block must include its continuation line: {block!r}"


def test_recipe_returns_only_the_tab_indented_lines(makefile: pathlib.Path) -> None:
    """A recipe is the rule's tab-indented lines, without the rule line."""
    assert makefile_recipe("lint", makefile=makefile) == (
        "\t$(RUFF) check\n\t$(MAKE) lint-pylint\n"
    ), "the recipe must be the tab-indented lines under the rule"


@pytest.mark.parametrize(
    ("query", "name", "message"),
    [
        (makefile_pin, "TY_VERSION", "Makefile does not define TY_VERSION"),
        (makefile_variable_block, "DF12_TOOL", "Makefile does not define DF12_TOOL"),
        (makefile_recipe, "typecheck", "Makefile has no recipe for typecheck"),
    ],
    ids=["pin", "variable-block", "recipe"],
)
def test_an_absent_definition_fails_with_its_message(
    makefile: pathlib.Path,
    query: cabc.Callable[..., str],
    name: str,
    message: str,
) -> None:
    """A missing definition fails the test and names what was missing."""
    with pytest.raises(pytest.fail.Exception, match=message):
        query(name, makefile=makefile)


def test_a_missing_makefile_fails_explicitly(tmp_path: pathlib.Path) -> None:
    """An unreadable Makefile fails with its path rather than a raw OSError."""
    missing = tmp_path / "Makefile"

    with pytest.raises(pytest.fail.Exception, match=r"cannot read .*Makefile"):
        makefile_pin("RUFF_VERSION", makefile=missing)


def test_an_undecodable_makefile_fails_explicitly(tmp_path: pathlib.Path) -> None:
    """A Makefile that is not UTF-8 fails with its path, not a decode error."""
    path = tmp_path / "Makefile"
    path.write_bytes(b"RUFF_VERSION ?= \xff\n")

    with pytest.raises(pytest.fail.Exception, match="is not UTF-8"):
        makefile_pin("RUFF_VERSION", makefile=path)


def test_queries_never_start_a_process(
    makefile: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reading the Makefile is a pure query; only the command helpers spawn."""

    def refuse(*args: object, **kwargs: object) -> typ.NoReturn:
        pytest.fail(f"a query started a process: {args!r} {kwargs!r}")

    monkeypatch.setattr(_makefile.subprocess, "run", refuse)

    makefile_pin("RUFF_VERSION", makefile=makefile)
    makefile_variable_block("PYLINT_TOOL", makefile=makefile)
    makefile_recipe("lint", makefile=makefile)
