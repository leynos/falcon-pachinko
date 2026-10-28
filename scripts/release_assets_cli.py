#!/usr/bin/env -S uv run python
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Command-line entry point for the release-asset reconciliation commands.

``release.yml`` runs this module from the workspace root as
``python3 -m scripts.release_assets_cli``, with the runner's preinstalled
``python3`` and the standard library alone: it executes inside the job that
holds the PyPI publishing token, so it resolves no third-party code (Cyclopts
or falcon-pachinko) at release time, and every network call stays in the workflow's
``curl`` and ``gh`` steps. That is the documented exception to the scripting
standards' Cyclopts and Python 3.13 defaults. Each subcommand is one step of
``release.yml``: ``carryover`` (``draft-release``), ``stage-pypi``
(``publish-pypi``), then ``stage-github``, ``check-fetched``, and ``verify``
(``publish-release``), plus ``index-expected``, which the post-upload index
re-read polls until the listing catches up with that upload. The reconciliation
logic itself lives in ``scripts/release_assets.py``; this module only wires it
to ``argparse`` and the workflow's ``GITHUB_OUTPUT`` protocol.
"""

from __future__ import annotations

import argparse
import sys
import typing as typ
from pathlib import Path

from scripts.release_assets import (
    ReleaseAssetError,
    State,
    carryover,
    check_fetched,
    local_names,
    missing_from_index,
    stage_github,
    stage_pypi,
    verify,
    write_output,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc


def _parser() -> argparse.ArgumentParser:
    """Build the command-line interface the release workflow calls."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("command", choices=_COMMANDS)
    parser.add_argument("--publish", type=Path, required=True)
    parser.add_argument("--assets", type=Path)
    parser.add_argument("--index", type=Path)
    parser.add_argument("--github-files", type=Path)
    parser.add_argument("--bundles", type=Path)
    parser.add_argument("--output", type=Path)
    # The post-upload index poll re-runs `index-expected` until it passes; a
    # failure there is the expected first answer, not a release failure, so it
    # must not paint an error annotation over a step that goes on to succeed.
    parser.add_argument("--poll", action="store_true")
    return parser


def _path(args: argparse.Namespace, option: str) -> Path:
    """Return a path option the chosen command requires."""
    value = getattr(args, option)
    if value is None:
        msg = f"--{option.replace('_', '-')} is required for {args.command}"
        raise ReleaseAssetError(msg)
    return value


def _carryover(state: State, args: argparse.Namespace) -> None:
    """Print the names to download from the GitHub Release, one per line."""
    del args
    for name in carryover(state):
        print(name)


def _stage_pypi(state: State, args: argparse.Namespace) -> None:
    """Stage PyPI's upload set and report whether anything remains."""
    write_output("remaining", value=stage_pypi(state, _path(args, "github_files")))


def _stage_github(state: State, args: argparse.Namespace) -> None:
    """Stage GitHub's upload set and print PyPI's files as TSV rows."""
    output = _path(args, "output")
    fetch = stage_github(state, _path(args, "bundles"), output)
    for name, url in fetch:
        print(f"{name}\t{url}")
    write_output("pending", value=bool(fetch) or any(output.iterdir()))


def _index_expected(state: State, args: argparse.Namespace) -> None:
    """Fail unless the index lists every name this run staged."""
    del args
    problems = missing_from_index(local_names(state.publish), state.pypi)
    if problems:
        msg = (
            "the PyPI index still does not list "
            f"{', '.join(problems)}; a CDN cache may be behind"
        )
        raise ReleaseAssetError(msg)


def _check_fetched(state: State, args: argparse.Namespace) -> None:
    """Check every fetched PyPI file against PyPI's digest."""
    check_fetched(state, _path(args, "output"))


def _verify(state: State, args: argparse.Namespace) -> None:
    """Fail on any artefact or bundle the two destinations disagree on."""
    problems = verify(state, _path(args, "bundles"))
    if problems:
        msg = "; ".join(problems)
        raise ReleaseAssetError(msg)
    print("PyPI and the GitHub Release hold the same bytes for every artefact.")


_COMMANDS: dict[str, cabc.Callable[[State, argparse.Namespace], None]] = {
    "carryover": _carryover,
    "stage-pypi": _stage_pypi,
    "stage-github": _stage_github,
    "check-fetched": _check_fetched,
    "index-expected": _index_expected,
    "verify": _verify,
}


_REQUIRES: dict[str, tuple[str, ...]] = {
    "carryover": ("assets",),
    "stage-pypi": ("index", "assets", "github_files"),
    "stage-github": ("index", "assets", "bundles", "output"),
    "check-fetched": ("index", "assets", "output"),
    "index-expected": ("index",),
    "verify": ("index", "assets", "bundles"),
}


def main(argv: cabc.Sequence[str] | None = None) -> int:
    """Run one reconciliation command; report failures as annotations.

    Parameters
    ----------
    argv : collections.abc.Sequence[str] | None, optional
        Command-line arguments to parse. If ``None``, parse the process
        arguments from ``sys.argv``.

    Returns
    -------
    int
        ``0`` when the selected command succeeds, or ``1`` when a release
        asset or input-file error is reported.
    """
    args = _parser().parse_args(argv)
    try:
        for option in _REQUIRES[args.command]:
            _path(args, option)
        state = State.load(args.publish, args.index, args.assets)
        _COMMANDS[args.command](state, args)
    except (ReleaseAssetError, OSError, KeyError, ValueError) as error:
        if args.poll:
            print(error, file=sys.stderr)
        else:
            print(f"::error title=release-assets::{error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
