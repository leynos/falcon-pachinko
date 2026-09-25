"""What `fmt` and `check-fmt` actually do, rather than what they say.

`test_markdown_baseline` reads the Makefile and asserts which command each
recipe names. That is a claim about text, and text was exactly what was wrong
here before: the Markdown target named `markdownlint`, a program that is not
installed, so it expanded to nothing, ran `xargs` with no command and exited
zero having linted the empty set. A reader looking at the recipe would have
said it linted Markdown. It did not.

So these rules do not read the Makefile at all. Two ask Make itself what it
would run, which replaces a hand-rolled variable expansion with the expander
that actually matters. Two run the targets for real against recording stubs on
`PATH`, which is the only way to see that the tool is invoked and that its
failure reaches Make.

The stubs make a real run safe: every tool the targets call is replaced, so
`fmt` rewrites nothing.

Run with:

    uv run pytest tests/workflow_contracts
"""

from __future__ import annotations

import os
import pathlib
import shutil
import subprocess  # ruff: ignore[suspicious-subprocess-import] - runs make against recording stubs

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
#: Every command the two targets invoke by bare name, and so every command a
#: real run must have a stub for. `make` resolves these through `PATH`, which
#: is what lets a stub stand in for one. Ruff is reached through `uv tool run`
#: at a pinned version rather than by its own name, so `uv` is the command to
#: stub: a `ruff` stub alone would be bypassed and the real formatter would
#: rewrite the checkout.
STUBBED_TOOLS = ("uv", "mdtablefix", "markdownlint-cli2")
#: The flags that select the Markdown set.
SELECT_FLAGS = ("--git", "--include-untracked")

MAKE_TIMEOUT = 300
#: Resolved once, from the ambient PATH, so the stub directory a real run
#: prepends cannot shadow Make itself. Stubbing the tools is the point; a
#: stubbed `make` would prove nothing.
MAKE = shutil.which("make")


def _dry_run(target: str) -> list[str]:
    """Return the commands Make would run for *target*, expanded by Make.

    Parameters
    ----------
    target : str
        The Make target.

    Returns
    -------
    list[str]
        One entry per command line, with every variable already expanded.
    """
    assert MAKE, "make must be installed to run these rules"
    # S603 is about untrusted input reaching a process. The executable is
    # resolved from PATH once at import and the only interpolated value is a
    # Make target from this module's own constants; no shell is involved.
    result = subprocess.run(  # ruff: ignore[subprocess-without-shell-equals-true] - see the comment above
        [MAKE, "--dry-run", target],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=MAKE_TIMEOUT,
    )
    assert result.returncode == 0, (
        f"make --dry-run {target} must succeed; it said {result.stderr!r}"
    )
    return [line.strip() for line in result.stdout.split("\n") if line.strip()]


def _invocations(commands: list[str], tool: str) -> list[str]:
    """Return the command lines where *tool* is the command being run.

    A recipe line may chain with ``;``, as the linter's does behind an
    ``unset``, so each segment is considered and the tool must be that
    segment's first word. Matching the tool anywhere in the line would accept
    it as an argument to something else, which is the reading a substring
    contract already gives and the one these rules exist to replace.

    Parameters
    ----------
    commands : list[str]
        Expanded command lines.
    tool : str
        The command name.

    Returns
    -------
    list[str]
        The matching lines.
    """
    matched = []
    for line in commands:
        for segment in line.split(";"):
            words = segment.split()
            if words and pathlib.PurePath(words[0]).name == tool:
                matched.append(line)
                break
    return matched


@pytest.fixture
def stub_bin(tmp_path: pathlib.Path) -> pathlib.Path:
    """Return a directory of recording stubs for every tool the targets call.

    Each stub appends its own name and arguments to ``calls.log`` beside it and
    exits zero, so a real run of either target invokes nothing that writes to
    the repository.

    Parameters
    ----------
    tmp_path : pathlib.Path
        pytest's per-test temporary directory.

    Returns
    -------
    pathlib.Path
        The directory to prepend to ``PATH``.
    """
    directory = tmp_path / "bin"
    directory.mkdir()
    log = directory / "calls.log"
    for tool in STUBBED_TOOLS:
        stub = directory / tool
        stub.write_text(
            f'#!/bin/sh\nprintf "%s %s\\n" "{tool}" "$*" >> "{log}"\nexit 0\n',
            encoding="utf-8",
        )
        stub.chmod(0o755)
    return directory


def _run_make(target: str, stub_bin: pathlib.Path) -> subprocess.CompletedProcess[str]:
    """Run one Make target with the stub directory first on ``PATH``.

    Parameters
    ----------
    target : str
        The Make target.
    stub_bin : pathlib.Path
        The stub directory.

    Returns
    -------
    subprocess.CompletedProcess[str]
        The completed process.
    """
    environment = dict(os.environ)
    environment["PATH"] = f"{stub_bin}{os.pathsep}{environment['PATH']}"
    assert MAKE, "make must be installed to run these rules"
    # S603 is about untrusted input reaching a process. The executable is
    # resolved from PATH once at import and the only interpolated value is a
    # Make target from this module's own constants; no shell is involved.
    return subprocess.run(  # ruff: ignore[subprocess-without-shell-equals-true] - see the comment above
        [MAKE, target],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
        env=environment,
        timeout=MAKE_TIMEOUT,
    )


def _calls(stub_bin: pathlib.Path) -> list[str]:
    """Return what the stubs recorded.

    Parameters
    ----------
    stub_bin : pathlib.Path
        The stub directory.

    Returns
    -------
    list[str]
        One entry per invocation.
    """
    log = stub_bin / "calls.log"
    if not log.exists():
        return []
    return [line for line in log.read_text(encoding="utf-8").split("\n") if line]


def test_make_expands_check_fmt_to_a_table_check() -> None:
    """Ask Make what `check-fmt` runs, rather than parsing the Makefile.

    A contract that expands `$(MDTABLEFIX)` itself is asserting its own
    expander as much as the Makefile. Make's `--dry-run` is the expander that
    decides what runs.
    """
    invocations = _invocations(_dry_run("check-fmt"), "mdtablefix")

    assert invocations, "check-fmt must run mdtablefix"
    checking = [line for line in invocations if "--check" in line]
    assert checking, f"check-fmt must run mdtablefix --check; it runs {invocations}"
    for line in checking:
        for flag in SELECT_FLAGS:
            assert flag in line, f"check-fmt must pass {flag}; it runs {line!r}"


def test_make_expands_fmt_to_a_rewrite_and_a_lint_fix() -> None:
    """Ask Make what `fmt` runs.

    Both tools, expanded. The linter's name is the one that was wrong before,
    and Make resolving it to an empty string is precisely how that failure
    looked.
    """
    commands = _dry_run("fmt")
    rewriting = [
        line for line in _invocations(commands, "mdtablefix") if "--in-place" in line
    ]
    fixing = [
        line for line in _invocations(commands, "markdownlint-cli2") if "--fix" in line
    ]

    assert rewriting, f"fmt must run mdtablefix --in-place; it runs {commands}"
    assert fixing, f"fmt must run markdownlint-cli2 --fix; it runs {commands}"
    for line in rewriting:
        for flag in SELECT_FLAGS:
            assert flag in line, f"fmt must pass {flag}; it runs {line!r}"


def test_check_fmt_really_invokes_the_table_check(stub_bin: pathlib.Path) -> None:
    """Run the target and see the tool called.

    The dry run shows what Make intends. Only a real run shows that the
    command reaches a program at all: an unset variable expands to nothing and
    the remaining arguments are then run as the command, or as arguments to
    whatever precedes them, and neither is visible from the recipe.
    """
    result = _run_make("check-fmt", stub_bin)
    calls = _calls(stub_bin)

    assert result.returncode == 0, (
        f"check-fmt must pass with stubbed tools; it said {result.stderr!r}"
    )
    checking = [
        line for line in calls if line.startswith("mdtablefix") and "--check" in line
    ]
    assert checking, f"mdtablefix --check was never invoked; the stubs saw {calls}"


def test_fmt_really_invokes_both_tools(stub_bin: pathlib.Path) -> None:
    """Run `fmt` and see both tools called.

    Safe because every tool it calls is stubbed, so nothing is rewritten.
    """
    result = _run_make("fmt", stub_bin)
    calls = _calls(stub_bin)

    assert result.returncode == 0, (
        f"fmt must pass with stubbed tools; it said {result.stderr!r}"
    )
    assert [
        line for line in calls if line.startswith("mdtablefix") and "--in-place" in line
    ], f"mdtablefix --in-place was never invoked; the stubs saw {calls}"
    assert [
        line
        for line in calls
        if line.startswith("markdownlint-cli2") and "--fix" in line
    ], f"markdownlint-cli2 --fix was never invoked; the stubs saw {calls}"


def test_check_fmt_fails_when_the_table_check_fails(stub_bin: pathlib.Path) -> None:
    """Let the tool's verdict reach Make.

    This is the rule the whole baseline rests on, and the one a text contract
    can only approximate. A recipe line prefixed with `-`, or piped into
    anything, reports success whatever the tool found, and the target then
    passes while the repository is unformatted. Making the stub fail is the
    only way to see the difference.
    """
    failing = stub_bin / "mdtablefix"
    failing.write_text("#!/bin/sh\nexit 3\n", encoding="utf-8")
    failing.chmod(0o755)

    result = _run_make("check-fmt", stub_bin)

    assert result.returncode != 0, (
        "check-fmt must fail when mdtablefix does; its status did not reach "
        f"Make. stdout={result.stdout!r}"
    )
