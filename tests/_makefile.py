"""Read and expand the repository Makefile for toolchain tests.

The toolchain tests assert on the Makefile's pins and recipes, and the
integration tests run the Makefile's own tool commands rather than a copy that
could drift from them. These helpers keep that parsing in one place.
"""

from __future__ import annotations

import os
import pathlib
import re
import shlex
import shutil
import subprocess  # ruff: ignore[suspicious-subprocess-import] - the one place tests spawn trusted repo tooling

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
MAKEFILE = REPO_ROOT / "Makefile"

# Make and uv read these from the environment. A test invoked under `make test`
# inherits the outer make's job server, and `uv run` exports the project
# virtual environment; neither belongs in a lint command run on its own.
_INHERITED_ONLY = ("MAKEFLAGS", "MFLAGS", "MAKELEVEL", "VIRTUAL_ENV")

type CompletedRun = subprocess.CompletedProcess[str]


def makefile_pin(variable: str) -> str:
    """Return the value of a single-line ``VARIABLE ?= value`` pin."""
    text = MAKEFILE.read_text(encoding="utf-8")
    match = re.search(
        rf"^{re.escape(variable)}\s*\?=\s*(\S+)\s*$", text, flags=re.MULTILINE
    )
    if match is None:
        pytest.fail(f"Makefile does not define {variable}")
    return match.group(1)


def makefile_variable_block(variable: str) -> str:
    """Return a Makefile assignment including its backslash continuations."""
    text = MAKEFILE.read_text(encoding="utf-8")
    match = re.search(
        rf"^{re.escape(variable)}\s*[:?]?=(?:[^\n]*\\\n)*[^\n]*$",
        text,
        flags=re.MULTILINE,
    )
    if match is None:
        pytest.fail(f"Makefile does not define {variable}")
    return match.group(0)


def makefile_recipe(target: str) -> str:
    """Return the tab-indented recipe lines of a Makefile rule."""
    text = MAKEFILE.read_text(encoding="utf-8")
    match = re.search(
        rf"^{re.escape(target)}:[^\n]*\n((?:\t[^\n]*\n)+)", text, flags=re.MULTILINE
    )
    if match is None:
        pytest.fail(f"Makefile has no recipe for {target}")
    return match.group(1)


def tool_environment() -> dict[str, str]:
    """Return the caller's environment without make and virtualenv leakage."""
    return {
        key: value for key, value in os.environ.items() if key not in _INHERITED_ONLY
    }


def run_command(argv: list[str], *, env: dict[str, str]) -> CompletedRun:
    """Run a repository tool command from the repo root and capture its output.

    Every process the toolchain tests start goes through here. ``argv[0]`` is
    resolved to an absolute path on the given ``PATH``, and the arguments come
    from the repository Makefile and fixed test values, never outside input.

    Parameters
    ----------
    argv : list of str
        The command and its arguments; the first element is looked up on PATH.
    env : dict of str to str
        The complete environment for the child process.

    Returns
    -------
    subprocess.CompletedProcess of str
        The finished process with its captured standard output and error.
    """
    executable = shutil.which(argv[0], path=env.get("PATH"))
    if executable is None:
        pytest.fail(f"{argv[0]} must be on PATH to exercise the lint toolchain")
    return subprocess.run(  # ruff: ignore[subprocess-without-shell-equals-true] - argv is repo tooling, never outside input
        [executable, *argv[1:]],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def run_make(*arguments: str, env: dict[str, str] | None = None) -> CompletedRun:
    """Run GNU make in the repository root and capture its output."""
    return run_command(
        ["make", "--no-print-directory", *arguments],
        env=env if env is not None else tool_environment(),
    )


def make_expand(expression: str) -> str:
    """Expand a Make expression, such as ``$(PYLINT_TOOL)``, in the real Makefile.

    GNU make's ``--eval`` defines a throwaway rule that prints the expansion,
    so the test reads the Makefile's own values without adding a target to it.

    Parameters
    ----------
    expression : str
        A Make expression to expand, such as ``$(PYLINT_TOOL)``.

    Returns
    -------
    str
        The expansion, with surrounding whitespace removed.
    """
    result = run_make(
        "-s", f"--eval=__expand: ; @printf '%s\\n' \"{expression}\"", "__expand"
    )
    if result.returncode != 0:
        pytest.fail(f"cannot expand {expression}: {result.stderr}")
    return result.stdout.strip()


def split_command(text: str) -> tuple[dict[str, str], list[str]]:
    """Split a shell command into its leading ``NAME=value`` assignments and argv."""
    assignments: dict[str, str] = {}
    words = shlex.split(text)
    while words and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", words[0]):
        name, _, value = words.pop(0).partition("=")
        assignments[name] = value
    return assignments, words
