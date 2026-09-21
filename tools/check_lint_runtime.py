"""Verify the interpreter and packages inside a lint tool environment.

The Makefile runs Pylint in two isolated tool environments: classic checks on
PyPy 8.0.0's Python 3.12 build, and df12-python-lints on CPython 3.14. A
selector such as ``pypy`` or ``3.14`` does not prove which interpreter uv
resolved, and PyPy 8 ships Python 3.11 and 3.12 builds under the same release
number. The Makefile therefore runs this script with the exact interpreter and
package set that the lint command will use, before linting, so a wrong
interpreter or an unresolved dependency fails loudly instead of letting a lint
run appear green.

The script uses only the standard library, because the tool environments hold
nothing but Pylint and its dependencies, and it must run on both Python 3.12
and 3.14.

Examples
--------
The Makefile checks the classic pass environment with arguments such as
``--implementation pypy --python-version 3.12 --pypy-version 8.0.0``,
``--require-dist pylint==4.0.8 --require-dist astroid==4.0.4``, and
``--forbid-module df12_python_lints``. Exit status 1 means a mismatch.
"""

from __future__ import annotations

import argparse
import importlib.metadata
import importlib.util
import sys


def _parse_version(text: str) -> tuple[int, ...]:
    """Parse a dotted numeric version such as ``3.12`` or ``8.0.0``."""
    try:
        return tuple(int(part) for part in text.split("."))
    except ValueError:
        msg = f"expected a dotted numeric version, got {text!r}"
        raise argparse.ArgumentTypeError(msg) from None


def _parse_requirement(text: str) -> tuple[str, str | None]:
    """Split ``name==version`` into its parts; a bare name pins no version."""
    name, _, version = text.partition("==")
    return name.strip(), version.strip() or None


def _build_parser() -> argparse.ArgumentParser:
    """Build the command-line parser."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--implementation", required=True, choices=["cpython", "pypy"])
    parser.add_argument("--python-version", required=True, type=_parse_version)
    parser.add_argument("--pypy-version", type=_parse_version)
    parser.add_argument(
        "--require-dist", action="append", default=[], type=_parse_requirement
    )
    parser.add_argument("--require-module", action="append", default=[])
    parser.add_argument("--forbid-module", action="append", default=[])
    return parser


def _check_interpreter(args: argparse.Namespace) -> list[str]:
    """Return mismatches between the running interpreter and the request."""
    problems: list[str] = []
    implementation = sys.implementation.name
    if implementation != args.implementation:
        problems.append(
            f"implementation is {implementation!r}, expected {args.implementation!r}"
        )
    width = len(args.python_version)
    if tuple(sys.version_info[:width]) != args.python_version:
        actual = ".".join(map(str, sys.version_info[:width]))
        expected = ".".join(map(str, args.python_version))
        problems.append(f"Python version is {actual}, expected {expected}")
    if args.pypy_version is not None:
        pypy_info = getattr(sys, "pypy_version_info", None)
        width = len(args.pypy_version)
        actual_pypy = None if pypy_info is None else tuple(pypy_info[:width])
        if actual_pypy != args.pypy_version:
            expected = ".".join(map(str, args.pypy_version))
            problems.append(f"PyPy version is {actual_pypy}, expected {expected}")
    return problems


def _check_packages(args: argparse.Namespace) -> list[str]:
    """Return missing, mismatched, or forbidden packages."""
    problems: list[str] = []
    for name, version in args.require_dist:
        try:
            installed = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            problems.append(f"distribution {name!r} is not installed")
            continue
        if version is not None and installed != version:
            problems.append(f"{name} is {installed}, expected {version}")
    problems.extend(
        f"module {module!r} is not importable"
        for module in args.require_module
        if importlib.util.find_spec(module) is None
    )
    problems.extend(
        f"module {module!r} must not be importable here"
        for module in args.forbid_module
        if importlib.util.find_spec(module) is not None
    )
    return problems


def _describe_runtime(names: list[str]) -> str:
    """Summarise the interpreter and installed packages for the build log."""
    parts = [f"executable={sys.executable}", f"version={sys.version.split()[0]}"]
    parts.append(f"implementation={sys.implementation.name}")
    if (pypy_info := getattr(sys, "pypy_version_info", None)) is not None:
        parts.append("pypy=" + ".".join(map(str, pypy_info[:3])))
    for name in names:
        try:
            parts.append(f"{name}={importlib.metadata.version(name)}")
        except importlib.metadata.PackageNotFoundError:
            parts.append(f"{name}=<missing>")
    return " ".join(parts)


def main(argv: list[str] | None = None) -> int:
    """Check the runtime; return a process exit status."""
    args = _build_parser().parse_args(argv)
    names = [name for name, _ in args.require_dist]
    print(f"lint runtime: {_describe_runtime(names)}")
    print(f"lint runtime detail: {sys.version.replace(chr(10), ' ')}")
    if problems := _check_interpreter(args) + _check_packages(args):
        for problem in problems:
            print(f"lint runtime mismatch: {problem}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
