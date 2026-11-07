"""Verify a distribution directory against the build job's reported digests.

The release build runs an isolated PEP 517 backend, and every later job
consumes the wheel and sdist that the build job uploaded. This module binds
that handoff to the exact bytes the build job measured: it refuses a directory
holding anything other than one wheel and one sdist, and it refuses a name or
SHA-256 digest that differs from the build job's report. Publication therefore
never uploads a file the build job did not produce.
"""

from __future__ import annotations

import argparse
import dataclasses as dc
import hashlib
import pathlib
import sys


class DistributionVerificationError(AssertionError):
    """A distribution directory does not match the build job's report."""


class DistributionCountError(DistributionVerificationError):
    """A directory does not hold exactly one distribution of a kind."""

    def __init__(self, directory: pathlib.Path, description: str, count: int) -> None:
        """Build the exception message."""
        super().__init__(
            f"{directory} must hold exactly one {description}; found {count}"
        )


class UnexpectedFilesError(DistributionVerificationError):
    """A directory holds a file other than the expected pair."""

    def __init__(self, directory: pathlib.Path, names: tuple[str, ...]) -> None:
        """Build the exception message."""
        super().__init__(f"{directory} holds unexpected files: {', '.join(names)}")


class DistributionNameMismatchError(DistributionVerificationError):
    """A distribution file name differs from the build job's report."""

    def __init__(self, description: str, actual: str, expected: str) -> None:
        """Build the exception message."""
        super().__init__(f"{description} is {actual!r}, expected {expected!r}")


class DistributionDigestMismatchError(DistributionVerificationError):
    """A distribution's bytes differ from the build job's report."""

    def __init__(self, description: str, name: str, actual: str, expected: str) -> None:
        """Build the exception message."""
        super().__init__(
            f"{description} {name} has sha256 {actual}, expected {expected}"
        )


@dc.dataclass(frozen=True, slots=True)
class DistributionIdentity:
    """The file name and SHA-256 digest reported for one distribution.

    Attributes
    ----------
    name : str
        The file name the build job reported.
    sha256 : str
        The lowercase hexadecimal SHA-256 digest the build job reported.
    """

    name: str
    sha256: str


def sha256_of(path: pathlib.Path) -> str:
    """Return the lowercase hexadecimal SHA-256 digest of *path*.

    Parameters
    ----------
    path : pathlib.Path
        The file to hash.

    Returns
    -------
    str
        The digest as 64 lowercase hexadecimal characters.
    """
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _single_distribution(
    directory: pathlib.Path, pattern: str, description: str
) -> pathlib.Path:
    """Return the one file matching *pattern* in *directory*.

    Parameters
    ----------
    directory : pathlib.Path
        The directory to search.
    pattern : str
        The glob for the distribution suffix, such as ``*.whl``.
    description : str
        The distribution kind, named in a failure.

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
        raise DistributionCountError(directory, description, len(matches))
    return matches[0]


def _reject_unexpected_files(
    directory: pathlib.Path, expected: tuple[pathlib.Path, pathlib.Path]
) -> None:
    """Refuse a directory that holds a file other than the expected pair.

    Parameters
    ----------
    directory : pathlib.Path
        The directory to inspect.
    expected : tuple of pathlib.Path
        The two distribution paths the caller expects.

    Raises
    ------
    UnexpectedFilesError
        If the directory holds any other regular file.
    """
    allowed = {path.name for path in expected}
    unexpected = tuple(
        sorted(
            path.name
            for path in directory.iterdir()
            if path.is_file() and path.name not in allowed
        )
    )
    if unexpected:
        raise UnexpectedFilesError(directory, unexpected)


def _check_identity(
    actual: pathlib.Path, expected: DistributionIdentity, description: str
) -> None:
    """Check one distribution's name and digest against the build report.

    Parameters
    ----------
    actual : pathlib.Path
        The downloaded distribution.
    expected : DistributionIdentity
        The name and digest the build job reported.
    description : str
        The distribution kind, named in a failure.

    Raises
    ------
    DistributionNameMismatchError
        If the file name differs from the build job's report.
    DistributionDigestMismatchError
        If the digest differs from the build job's report.
    """
    if actual.name != expected.name:
        raise DistributionNameMismatchError(description, actual.name, expected.name)
    actual_digest = sha256_of(actual)
    if actual_digest != expected.sha256:
        raise DistributionDigestMismatchError(
            description, actual.name, actual_digest, expected.sha256
        )


def verify(
    directory: pathlib.Path,
    *,
    wheel: DistributionIdentity,
    sdist: DistributionIdentity,
) -> None:
    """Check the wheel and sdist in *directory* against the build report.

    Parameters
    ----------
    directory : pathlib.Path
        The directory holding the downloaded distributions.
    wheel : DistributionIdentity
        The wheel name and digest the build job reported.
    sdist : DistributionIdentity
        The source distribution name and digest the build job reported.

    """
    wheel_path = _single_distribution(directory, "*.whl", "wheel")
    sdist_path = _single_distribution(directory, "*.tar.gz", "source distribution")
    _reject_unexpected_files(directory, (wheel_path, sdist_path))
    _check_identity(wheel_path, wheel, "wheel")
    _check_identity(sdist_path, sdist, "source distribution")


def _parser() -> argparse.ArgumentParser:
    """Return the command-line parser for the verification check."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=pathlib.Path, required=True)
    parser.add_argument("--wheel-name", required=True)
    parser.add_argument("--wheel-sha256", required=True)
    parser.add_argument("--sdist-name", required=True)
    parser.add_argument("--sdist-sha256", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the verification check and return a process exit status.

    Parameters
    ----------
    argv : list of str, optional
        The command-line arguments; defaults to ``sys.argv[1:]``.

    Returns
    -------
    int
        Zero when the distributions match the build report, otherwise one.
    """
    args = _parser().parse_args(argv)
    try:
        verify(
            args.directory,
            wheel=DistributionIdentity(args.wheel_name, args.wheel_sha256),
            sdist=DistributionIdentity(args.sdist_name, args.sdist_sha256),
        )
    except DistributionVerificationError as error:
        print(f"::error::{error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
