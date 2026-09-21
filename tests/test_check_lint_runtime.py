"""Unit tests for tools/check_lint_runtime.py.

The Makefile runs this checker inside the PyPy and CPython tool environments,
and tests/test_lint_toolchain_integration.py covers that end to end. These
tests exercise its mismatch detection in-process against the running
interpreter, so every rejection path is pinned without a subprocess.
"""

from __future__ import annotations

import argparse
import importlib.metadata
import sys

import pytest

from tools import check_lint_runtime

CURRENT_PYTHON = f"{sys.version_info.major}.{sys.version_info.minor}"


def _run(capsys: pytest.CaptureFixture[str], *extra: str) -> tuple[int, str, str]:
    """Run the checker against this interpreter and capture its output."""
    status = check_lint_runtime.main([
        "--implementation",
        sys.implementation.name,
        "--python-version",
        CURRENT_PYTHON,
        *extra,
    ])
    captured = capsys.readouterr()
    return status, captured.out, captured.err


def test_matching_runtime_passes_and_records_itself(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The running interpreter satisfies a request describing itself."""
    pytest_version = importlib.metadata.version("pytest")

    status, out, err = _run(capsys, "--require-dist", f"pytest=={pytest_version}")

    assert status == 0, f"a matching runtime must pass, but reported:\n{err}"
    assert f"implementation={sys.implementation.name}" in out, (
        "the record must name the implementation"
    )
    assert f"pytest={pytest_version}" in out, "the record must list checked packages"


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        (
            ["--implementation", "pypy"],
            f"implementation is '{sys.implementation.name}', expected 'pypy'",
        ),
        (["--python-version", "2.7"], f"Python version is {CURRENT_PYTHON}"),
        (["--pypy-version", "8.0.0"], "PyPy version is None, expected 8.0.0"),
        (
            ["--require-dist", "no-such-distribution"],
            "distribution 'no-such-distribution' is not installed",
        ),
        (["--require-dist", "pytest==0.0.0"], "expected 0.0.0"),
        (
            ["--require-module", "no_such_module"],
            "module 'no_such_module' is not importable",
        ),
        (["--forbid-module", "json"], "module 'json' must not be importable here"),
    ],
    ids=[
        "wrong-implementation",
        "wrong-python",
        "not-pypy",
        "missing-distribution",
        "wrong-distribution-version",
        "missing-module",
        "forbidden-module",
    ],
)
def test_mismatch_fails_with_a_clear_reason(
    capsys: pytest.CaptureFixture[str], arguments: list[str], message: str
) -> None:
    """Each kind of mismatch fails the check and names what was wrong."""
    # Later arguments override the matching defaults `_run` supplies first.
    status, _, err = _run(capsys, *arguments)

    assert status == 1, "a mismatched runtime must fail the check"
    assert message in err, f"stderr must explain the mismatch:\n{err}"


def test_missing_distribution_is_recorded_as_missing(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The build-log record marks an absent package instead of crashing."""
    _, out, _ = _run(capsys, "--require-dist", "no-such-distribution")

    assert "no-such-distribution=<missing>" in out, (
        "the record must show which package is missing"
    )


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("pylint==4.0.8", ("pylint", "4.0.8")),
        ("df12-python-lints", ("df12-python-lints", None)),
    ],
    ids=["pinned", "bare-name"],
)
def test_requirement_parsing(text: str, expected: tuple[str, str | None]) -> None:
    """A requirement pins a version only when it carries one."""
    assert check_lint_runtime._parse_requirement(text) == expected, (
        f"{text!r} must parse to {expected}"
    )


def test_non_numeric_version_is_rejected() -> None:
    """A version such as ``3.x`` is a usage error, not a silent mismatch."""
    with pytest.raises(argparse.ArgumentTypeError, match="dotted numeric version"):
        check_lint_runtime._parse_version("3.x")
