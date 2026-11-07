"""Build the release wheel twice and prove the two builds agree.

The release wheel is built from the sdist, so the sdist is the input a
consumer sees. Building the wheel twice from that same sdist and comparing the
two SHA-256 digests catches a recipe that embeds a timestamp, a random
identifier, or an ordering that varies between runs.

The recipe lives here rather than in the Makefile so each failure branch can be
tested without invoking a real build.
"""

from __future__ import annotations

import argparse
import os
import pathlib
import shutil
import subprocess  # ruff: ignore[suspicious-subprocess-import] - runs the repository's release build
import sys
import tempfile

from scripts.verify_build_distributions import sha256_of

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
DEFAULT_CONSTRAINTS = "build-constraints.txt"
_BUILD_FLAGS = ("--no-cache", "--require-build-hashes")


class ReleaseBuildError(RuntimeError):
    """A release build did not produce the artefact the recipe expects."""


class DistributionCountError(ReleaseBuildError):
    """A directory does not hold exactly one distribution of a kind."""

    def __init__(self, directory: pathlib.Path, pattern: str, count: int) -> None:
        """Build the exception message."""
        super().__init__(
            f"{directory} must hold exactly one {pattern} file; found {count}"
        )


class WheelNameMismatchError(ReleaseBuildError):
    """The two wheel builds produced different file names."""

    def __init__(self, first: pathlib.Path, second: pathlib.Path) -> None:
        """Build the exception message."""
        super().__init__(f"wheel names differ: {first.name} != {second.name}")


class WheelDigestMismatchError(ReleaseBuildError):
    """The two wheel builds produced different bytes."""

    def __init__(
        self,
        first: pathlib.Path,
        first_digest: str,
        second: pathlib.Path,
        second_digest: str,
    ) -> None:
        """Build the exception message."""
        super().__init__(
            f"wheel digests differ: {first.name} {first_digest} != "
            f"{second.name} {second_digest}"
        )


def build_command(*arguments: str, constraints: str) -> list[str]:
    """Return the uv command for one isolated, hash-verified build.

    Parameters
    ----------
    *arguments : str
        The build arguments, such as ``--sdist`` or the sdist path.
    constraints : str
        The constraints file naming and hashing every build requirement.

    Returns
    -------
    list of str
        The command, including the constraint and integrity flags. Every
        build goes through this function, so a new invocation cannot quietly
        drop one of them.
    """
    return [
        "uv",
        "build",
        *arguments,
        "--build-constraint",
        constraints,
        *_BUILD_FLAGS,
    ]


def run_build(*arguments: str, constraints: str, environment: dict[str, str]) -> None:
    """Run one isolated, hash-verified uv build from the repository root.

    Parameters
    ----------
    *arguments : str
        The build arguments, such as ``--sdist`` or the sdist path.
    constraints : str
        The constraints file naming and hashing every build requirement.
    environment : dict of str to str
        The environment for the build; it carries ``SOURCE_DATE_EPOCH``.
    """
    subprocess.run(  # ruff: ignore[subprocess-without-shell-equals-true] - executes only the configured uv command
        build_command(*arguments, constraints=constraints),
        check=True,
        cwd=REPO_ROOT,
        env=environment,
    )


def source_date_epoch() -> str:
    """Return the current commit's timestamp for reproducible artefacts.

    Returns
    -------
    str
        The commit time as whole seconds since the Unix epoch.
    """
    completed = subprocess.run(
        # ruff: ignore[start-process-with-partial-path] - runs a fixed git query
        ["git", "log", "-1", "--format=%ct"],
        check=True,
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
    )
    return completed.stdout.strip()


def clear_build_state() -> None:
    """Remove the working files a previous build may have left behind.

    A stale ``build`` tree or ``*.egg-info`` directory can change what the
    next build packages, which would make the comparison meaningless.
    """
    shutil.rmtree(REPO_ROOT / "build", ignore_errors=True)
    for egg_info in REPO_ROOT.glob("*.egg-info"):
        shutil.rmtree(egg_info, ignore_errors=True)


def single_distribution(directory: pathlib.Path, pattern: str) -> pathlib.Path:
    """Return the one file matching *pattern* in *directory*.

    Parameters
    ----------
    directory : pathlib.Path
        The directory to search.
    pattern : str
        The glob for the distribution suffix, such as ``*.whl``.

    Returns
    -------
    pathlib.Path
        The single matching file.

    Raises
    ------
    DistributionCountError
        If the directory holds zero or more than one match.
    """
    matches = sorted(directory.glob(pattern))
    if len(matches) != 1:
        raise DistributionCountError(directory, pattern, len(matches))
    return matches[0]


def compare_wheels(first: pathlib.Path, second: pathlib.Path) -> str:
    """Require two independently built wheels to be identical.

    Parameters
    ----------
    first : pathlib.Path
        The wheel built into the output directory.
    second : pathlib.Path
        The wheel built into the repeat directory.

    Returns
    -------
    str
        The shared SHA-256 digest.

    Raises
    ------
    WheelDigestMismatchError
        If the bytes differ.
    WheelNameMismatchError
        If the file names differ.
    """
    if first.name != second.name:
        raise WheelNameMismatchError(first, second)
    first_digest = sha256_of(first)
    second_digest = sha256_of(second)
    if first_digest != second_digest:
        raise WheelDigestMismatchError(first, first_digest, second, second_digest)
    return first_digest


def build_release(
    output_dir: pathlib.Path, *, constraints: str = DEFAULT_CONSTRAINTS
) -> str:
    """Build the sdist, then build its wheel twice and compare the digests.

    Parameters
    ----------
    output_dir : pathlib.Path
        Where the sdist and the first wheel are written. A relative path
        resolves against the repository root, matching ``make``.
    constraints : str
        The constraints file naming and hashing every build requirement.

    Returns
    -------
    str
        The shared SHA-256 digest of the two wheels.
    """
    directory = output_dir if output_dir.is_absolute() else REPO_ROOT / output_dir
    directory.mkdir(parents=True, exist_ok=True)
    for artefact in (*directory.glob("*.whl"), *directory.glob("*.tar.gz")):
        artefact.unlink()
    environment = {**os.environ, "SOURCE_DATE_EPOCH": source_date_epoch()}
    clear_build_state()
    run_build(
        "--sdist",
        "--out-dir",
        str(directory),
        constraints=constraints,
        environment=environment,
    )
    sdist = single_distribution(directory, "*.tar.gz")
    with tempfile.TemporaryDirectory(
        prefix=f"{directory.name}-repeat.", dir=directory.parent
    ) as repeat:
        repeat_dir = pathlib.Path(repeat)
        clear_build_state()
        run_build(
            str(sdist),
            "--wheel",
            "--out-dir",
            str(directory),
            constraints=constraints,
            environment=environment,
        )
        clear_build_state()
        run_build(
            str(sdist),
            "--wheel",
            "--out-dir",
            str(repeat_dir),
            constraints=constraints,
            environment=environment,
        )
        first = single_distribution(directory, "*.whl")
        second = single_distribution(repeat_dir, "*.whl")
        return compare_wheels(first, second)


def _parser() -> argparse.ArgumentParser:
    """Return the command-line parser for the release build."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=pathlib.Path, default=pathlib.Path("dist"))
    parser.add_argument("--constraints", default=DEFAULT_CONSTRAINTS)
    return parser


def main(argv: list[str] | None = None) -> int:
    """Build the release wheel twice and report the shared digest.

    Parameters
    ----------
    argv : list of str, optional
        The command-line arguments; defaults to ``sys.argv[1:]``.

    Returns
    -------
    int
        Zero when both builds agree, otherwise one.
    """
    arguments = _parser().parse_args(argv)
    try:
        digest = build_release(arguments.output_dir, constraints=arguments.constraints)
    except ReleaseBuildError as error:
        print(f"::error::{error}", file=sys.stderr)
        return 1
    except subprocess.CalledProcessError as error:
        print(f"::error::build command failed: {error.cmd}", file=sys.stderr)
        return 1
    print(f"release wheel sha256: {digest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
