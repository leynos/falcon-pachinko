"""Check release archives and an isolated wheel installation."""

from __future__ import annotations

import email.parser
import os
import pathlib
import shutil
import subprocess  # ruff: ignore[suspicious-subprocess-import] - run the build and installation tools
import sys
import tarfile
import tempfile
import typing as typ
import zipfile

import pytest

if typ.TYPE_CHECKING:
    import collections.abc as cabc

ROOT = pathlib.Path(__file__).resolve().parents[1]
UV = shutil.which("uv")
pytestmark = pytest.mark.skipif(
    UV is None, reason="uv is required to build distributions"
)


def _run(arguments: list[str], cwd: pathlib.Path) -> subprocess.CompletedProcess[str]:
    """Run a fixed tool invocation and retain its diagnostic output."""
    env = os.environ.copy()
    for key in list(env):
        if key.startswith("GIT_CONFIG_") or key in {
            "GH_TOKEN",
            "GITHUB_TOKEN",
            "BASH_ENV",
            "GIT_SSH",
            "GIT_SSH_COMMAND",
        }:
            env.pop(key)
    lody = str(pathlib.Path.home() / ".lody")
    env["PATH"] = os.pathsep.join(
        part
        for part in env.get("PATH", "").split(os.pathsep)
        if part != lody and not part.startswith(lody + os.sep)
    )
    return subprocess.run(  # ruff: ignore[subprocess-without-shell-equals-true] - fixed tool argv
        arguments,
        cwd=cwd,
        env=env,
        check=False,
        capture_output=True,
        text=True,
        timeout=300,
    )


@pytest.fixture(scope="module")
def distributions() -> cabc.Iterator[tuple[pathlib.Path, pathlib.Path]]:
    """Build one sdist and wheel in a workspace-local scratch directory."""
    cache = ROOT / ".cache"
    cache.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="release-archives-", dir=cache) as scratch:
        output = pathlib.Path(scratch) / "dist"
        result = _run([str(UV), "build", "--out-dir", str(output)], ROOT)
        assert result.returncode == 0, result.stdout + result.stderr
        sdists = list(output.glob("*.tar.gz"))
        wheels = list(output.glob("*.whl"))
        assert len(sdists) == len(wheels) == 1, (sdists, wheels)
        yield sdists[0], wheels[0]


def _package_files() -> set[str]:
    """Derive shippable package modules and data from the checkout."""
    return {
        path.relative_to(ROOT).as_posix()
        for path in (ROOT / "falcon_pachinko").rglob("*")
        if path.is_file()
        and path.suffix in {".py", ".typed"}
        and not {"behaviour", "unittests", "__pycache__"}.intersection(path.parts)
    }


def test_archives_contain_package_and_wheel_metadata(
    distributions: tuple[pathlib.Path, pathlib.Path],
) -> None:
    """Every shipped source file survives both archives and metadata is correct."""
    sdist, wheel = distributions
    expected = _package_files()
    assert expected, "source package files must be discovered"
    with tarfile.open(sdist, "r:gz") as archive:
        names = {name.partition("/")[2] for name in archive.getnames()}
    assert expected <= names, f"sdist is missing {sorted(expected - names)}"
    with zipfile.ZipFile(wheel) as archive:
        names = set(archive.namelist())
        metadata_name = next(
            name for name in names if name.endswith(".dist-info/METADATA")
        )
        metadata = email.parser.BytesParser().parsebytes(archive.read(metadata_name))
    assert expected <= names, f"wheel is missing {sorted(expected - names)}"
    assert not any(
        name.startswith(("falcon_pachinko/behaviour/", "falcon_pachinko/unittests/"))
        for name in names
    ), "test packages must not ship in wheel"
    assert metadata["Name"] == "falcon-pachinko", metadata["Name"]
    requirements = metadata.get_all("Requires-Dist", [])
    assert any(item.startswith("falcon") for item in requirements), requirements
    assert any(item.startswith("msgspec") for item in requirements), requirements


def test_wheel_imports_without_pytest(
    distributions: tuple[pathlib.Path, pathlib.Path],
) -> None:
    """A clean wheel installation imports both packages and the fixture fallback."""
    wheel = distributions[1]
    root = wheel.parent.parent
    venv = root / "venv"
    result = _run([str(UV), "venv", str(venv)], root)
    assert result.returncode == 0, result.stdout + result.stderr
    python = venv / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    result = _run(
        [str(UV), "pip", "install", "--python", str(python), str(wheel)], root
    )
    assert result.returncode == 0, result.stdout + result.stderr
    script = """\
import pathlib
import falcon_pachinko
import falcon_pachinko.testing
import falcon_pachinko.testing.fixtures as fixtures
for module in (falcon_pachinko, falcon_pachinko.testing, fixtures):
    assert 'site-packages' in pathlib.Path(module.__file__).parts, module.__file__
assert fixtures.pytest is None
assert fixtures.websocket_simulator is fixtures._websocket_simulator
"""
    result = _run([str(python), "-I", "-c", script], root)
    assert result.returncode == 0, result.stdout + result.stderr
