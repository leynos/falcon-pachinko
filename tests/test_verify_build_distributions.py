"""Behaviour of the release distribution verification helper.

The helper is the gate between the build job's digest report and every job
that publishes the distributions. These tests drive both the happy path and
each way a directory can disagree with that report.
"""

from __future__ import annotations

import typing as typ

import pytest

from scripts.verify_build_distributions import (
    DistributionIdentity,
    DistributionVerificationError,
    main,
    sha256_of,
    verify,
)

if typ.TYPE_CHECKING:
    import pathlib

WHEEL = "falcon_pachinko-0.1.0a3-py3-none-any.whl"
SDIST = "falcon_pachinko-0.1.0a3.tar.gz"


@pytest.fixture
def publish_dir(tmp_path: pathlib.Path) -> pathlib.Path:
    """Provide a publish directory holding one wheel and one sdist."""
    directory = tmp_path / "publish"
    directory.mkdir()
    (directory / WHEEL).write_bytes(b"wheel bytes")
    (directory / SDIST).write_bytes(b"sdist bytes")
    return directory


def _identity(path: pathlib.Path) -> DistributionIdentity:
    """Return the name and digest of *path* as the build job reports them.

    Parameters
    ----------
    path : pathlib.Path
        A distribution file.

    Returns
    -------
    DistributionIdentity
        The file's name and SHA-256 digest.
    """
    return DistributionIdentity(path.name, sha256_of(path))


def _verify(directory: pathlib.Path, *, wheel_name: str = WHEEL) -> None:
    """Verify *directory* against the digests it currently holds.

    Parameters
    ----------
    directory : pathlib.Path
        The directory to verify.
    wheel_name : str
        The wheel name to report as expected.
    """
    verify(
        directory,
        wheel=DistributionIdentity(wheel_name, sha256_of(directory / WHEEL)),
        sdist=_identity(directory / SDIST),
    )


def test_matching_distributions_are_accepted(publish_dir: pathlib.Path) -> None:
    """The exact files the build reported pass the check."""
    verify(
        publish_dir,
        wheel=_identity(publish_dir / WHEEL),
        sdist=_identity(publish_dir / SDIST),
    )


def test_a_different_wheel_name_is_refused(publish_dir: pathlib.Path) -> None:
    """A renamed wheel is not the artefact the build job measured."""
    with pytest.raises(DistributionVerificationError, match="expected"):
        _verify(publish_dir, wheel_name="other.whl")


def test_a_different_digest_is_refused(publish_dir: pathlib.Path) -> None:
    """Different bytes under the same name are refused."""
    directory = publish_dir
    with pytest.raises(DistributionVerificationError, match="sha256"):
        verify(
            directory,
            wheel=DistributionIdentity(WHEEL, "0" * 64),
            sdist=_identity(directory / SDIST),
        )


def test_an_extra_distribution_is_refused(publish_dir: pathlib.Path) -> None:
    """A second wheel means the directory is not the reviewed build."""
    directory = publish_dir
    (directory / "extra.whl").write_bytes(b"extra")
    with pytest.raises(DistributionVerificationError, match="exactly one wheel"):
        _verify(directory)


def test_an_unexpected_file_is_refused(publish_dir: pathlib.Path) -> None:
    """A non-distribution beside the pair is also a handoff failure."""
    directory = publish_dir
    (directory / "notes.txt").write_text("unexpected", encoding="utf-8")
    with pytest.raises(DistributionVerificationError, match="unexpected files"):
        _verify(directory)


def test_a_missing_sdist_is_refused(publish_dir: pathlib.Path) -> None:
    """The release always travels with its source distribution."""
    directory = publish_dir
    (directory / SDIST).unlink()
    with pytest.raises(DistributionVerificationError, match="source distribution"):
        verify(
            directory,
            wheel=_identity(directory / WHEEL),
            sdist=DistributionIdentity(SDIST, "0" * 64),
        )


def test_the_cli_reports_a_refusal(
    publish_dir: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A refusal is a non-zero exit with a GitHub Actions error annotation."""
    directory = publish_dir
    status = main([
        "--directory",
        str(directory),
        "--wheel-name",
        "other.whl",
        "--wheel-sha256",
        sha256_of(directory / WHEEL),
        "--sdist-name",
        SDIST,
        "--sdist-sha256",
        sha256_of(directory / SDIST),
    ])
    assert status == 1, f"unexpected exit status: {status}"
    assert "::error::" in capsys.readouterr().err, (
        "the refusal must be annotated for GitHub Actions"
    )
