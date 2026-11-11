"""Behaviour of the two-build release wheel recipe.

``make release-wheel`` proves the wheel is deterministic: it builds the sdist
once, builds the wheel from that sdist twice, and compares the two SHA-256
digests. These tests drive each failure branch with fabricated build outputs,
so a broken comparison fails here rather than during a release.
"""

from __future__ import annotations

import pathlib

import pytest

from scripts import release_wheel
from scripts.release_wheel import (
    ReleaseBuildError,
    build_command,
    compare_wheels,
    main,
)
from scripts.verify_build_distributions import (
    DistributionVerificationError,
    sha256_of,
    single_distribution,
)

WHEEL = "falcon_pachinko-0.1.0a3-py3-none-any.whl"
SDIST = "falcon_pachinko-0.1.0a3.tar.gz"
REQUIRED_OPTIONS = (
    "--build-constraint",
    "build-constraints.txt",
    "--require-build-hashes",
    "--no-cache",
)


@pytest.fixture
def wheels(tmp_path: pathlib.Path) -> tuple[pathlib.Path, pathlib.Path]:
    """Provide two byte-identical wheel builds in separate directories."""
    paths = (tmp_path / "dist" / WHEEL, tmp_path / "repeat" / WHEEL)
    for path in paths:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"wheel bytes")
    return paths


def test_identical_wheels_share_a_digest(
    wheels: tuple[pathlib.Path, pathlib.Path],
) -> None:
    """Two builds of the same sdist pass and report their shared digest."""
    first, second = wheels
    assert compare_wheels(first, second) == sha256_of(first), (
        "the reported digest must be the wheel's own digest"
    )


def test_different_wheel_names_are_refused(tmp_path: pathlib.Path) -> None:
    """A renamed second wheel is not the same artefact."""
    first = tmp_path / "dist" / WHEEL
    second = tmp_path / "repeat" / "other.whl"
    for path in (first, second):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"wheel bytes")
    with pytest.raises(ReleaseBuildError, match="names differ"):
        compare_wheels(first, second)


def test_different_wheel_digests_are_refused(tmp_path: pathlib.Path) -> None:
    """Different bytes under the same name are not deterministic."""
    first = tmp_path / "dist" / WHEEL
    second = tmp_path / "repeat" / WHEEL
    first.parent.mkdir(parents=True)
    second.parent.mkdir(parents=True)
    first.write_bytes(b"first bytes")
    second.write_bytes(b"second bytes")
    with pytest.raises(ReleaseBuildError, match="digests differ"):
        compare_wheels(first, second)


def test_one_distribution_is_accepted(tmp_path: pathlib.Path) -> None:
    """A directory holding one match yields that file."""
    wheel = tmp_path / WHEEL
    wheel.write_bytes(b"wheel bytes")
    assert single_distribution(tmp_path, "*.whl", "wheel") == wheel, (
        "the only wheel in the directory must be returned"
    )


@pytest.mark.parametrize("names", [(), ("one.whl", "two.whl")], ids=["none", "two"])
def test_an_ambiguous_distribution_count_is_refused(
    tmp_path: pathlib.Path, names: tuple[str, ...]
) -> None:
    """Zero or two matches mean the build produced the wrong artefact."""
    for name in names:
        (tmp_path / name).write_bytes(b"wheel bytes")
    with pytest.raises(DistributionVerificationError, match="exactly one"):
        single_distribution(tmp_path, "*.whl", "wheel")


def test_every_build_command_is_verified() -> None:
    """Each build constrains, hashes and uncaches its isolated inputs."""
    command = build_command("--sdist", constraints="build-constraints.txt")
    missing = [option for option in REQUIRED_OPTIONS if option not in command]
    assert not missing, f"the build command lacks {missing}"
    assert "--no-build-isolation" not in command, "the release build must stay isolated"


class SyntheticBuildFailureError(ReleaseBuildError):
    """The substituted build reports a deterministic refusal."""

    def __init__(self) -> None:
        """Build the exception message."""
        super().__init__("the two wheels differ")


def _out_dir(arguments: tuple[str, ...]) -> pathlib.Path:
    """Return the directory named by a build's ``--out-dir`` argument.

    Parameters
    ----------
    arguments : tuple of str
        The arguments passed to one build.

    Returns
    -------
    pathlib.Path
        The build's output directory.
    """
    return pathlib.Path(arguments[arguments.index("--out-dir") + 1])


def test_build_release_builds_the_wheel_twice_from_the_sdist(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The recipe builds the sdist, then compares two wheels built from it."""
    output_dir = tmp_path / "dist"
    output_dir.mkdir()
    (output_dir / "old.whl").write_bytes(b"stale wheel")
    calls: list[tuple[tuple[str, ...], str, str]] = []

    def fake_build(
        *arguments: str, constraints: str, environment: dict[str, str]
    ) -> None:
        """Record the call and write the artefact the real build would."""
        calls.append((arguments, constraints, environment["SOURCE_DATE_EPOCH"]))
        out_dir = _out_dir(arguments)
        name = SDIST if "--sdist" in arguments else WHEEL
        (out_dir / name).write_bytes(b"artefact bytes")

    def fixed_epoch() -> str:
        """Return a fixed commit time for every build."""
        return "1"

    def ignore_clear() -> None:
        """Leave the build tree alone; the fake build writes nothing there."""

    monkeypatch.setattr(release_wheel, "run_build", fake_build)
    monkeypatch.setattr(release_wheel, "source_date_epoch", fixed_epoch)
    monkeypatch.setattr(release_wheel, "clear_build_state", ignore_clear)

    digest = release_wheel.build_release(output_dir)

    assert not (output_dir / "old.whl").exists(), (
        "a stale wheel must not survive into the comparison"
    )
    assert len(calls) == 3, f"expected three builds, got {len(calls)}"
    sdist_call, first_wheel, second_wheel = calls
    assert sdist_call[0][0] == "--sdist", "the first build must create the sdist"
    assert not any(argument.endswith(".tar.gz") for argument in sdist_call[0]), (
        "the sdist build must not read an existing sdist"
    )
    for description, call in (("first", first_wheel), ("second", second_wheel)):
        assert call[0][0].endswith(".tar.gz"), (
            f"the {description} wheel build must read the sdist"
        )
        assert "--wheel" in call[0], f"the {description} build must create a wheel"
    assert _out_dir(first_wheel[0]) != _out_dir(second_wheel[0]), (
        "the two wheel builds need separate output directories"
    )
    assert all(call[2] == "1" for call in calls), (
        "every build must share one SOURCE_DATE_EPOCH"
    )
    assert all(call[1] == "build-constraints.txt" for call in calls), (
        "every build must use the reviewed constraints file"
    )
    assert digest == sha256_of(output_dir / WHEEL), (
        "the function must report the shared wheel digest"
    )


def test_main_reports_a_refusal(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A comparison failure is a non-zero exit with an Actions annotation."""

    def fail(*_args: object, **_kwargs: object) -> str:
        """Stand in for a build whose two wheels disagree."""
        raise SyntheticBuildFailureError

    monkeypatch.setattr(release_wheel, "build_release", fail)
    status = main(["--output-dir", str(tmp_path)])
    assert status == 1, f"unexpected exit status: {status}"
    assert "::error::" in capsys.readouterr().err, (
        "the refusal must be annotated for GitHub Actions"
    )
