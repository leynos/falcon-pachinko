"""Shared subprocess support for duplication-gate workflow tests.

Adapted from ``leynos/episodic`` PR #276 at commit
``d9e5ac0d254f375e2986f52d91a3b88c117c833b``.
"""

import json
import os
import shutil
import subprocess  # ruff: ignore[suspicious-subprocess-import] - invokes fixed test commands
import sys
from collections import abc as cabc
from pathlib import Path

import duplication_allowlist as allowlist
import duplication_gate as gate
import nose_detector as detector
import nose_schema as schema

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]

#: Report emitted by the stub detector: one two-member duplication family.
STUB_REPORT: dict[str, object] = {
    "schema_version": 9,
    "families": [
        {
            "id": "stub",
            "witness": "copy-paste",
            "surface": "default",
            "value": 22.1,
            "metrics": {"mean_score": 1.0},
            "locations": [
                {"file": "falcon_pachinko/a.py", "start": 1, "end": 20, "name": None},
                {"file": "falcon_pachinko/b.py", "start": 30, "end": 49, "name": None},
            ],
        }
    ],
}


def copied_gate_workspace(tmp_path: Path) -> tuple[Path, Path]:
    """Create a mutable workspace containing the gate and its helper modules."""
    workspace = tmp_path / "gate-workspace"
    scripts = workspace / "scripts"
    scripts.mkdir(parents=True)
    package = workspace / "falcon_pachinko"
    package.mkdir()
    (package / "sample.py").write_text("VALUE = 1\n", encoding="utf-8")
    for name in (
        "atomic_write.py",
        "duplication_allowlist.py",
        "duplication_gate.py",
        "nose_detector.py",
        "nose_schema.py",
    ):
        shutil.copy(REPOSITORY_ROOT / "scripts" / name, scripts / name)
    (workspace / "pyproject.toml").write_text(
        '[project]\nname = "gate-test"\nversion = "0"\n', encoding="utf-8"
    )
    return workspace, scripts / "duplication_gate.py"


def write_stub_nose(
    directory: Path,
    *,
    version: str = "nose 0.20.0",
    report: dict[str, object] | None = None,
) -> Path:
    """Write an executable stub standing in for the pinned nose binary.

    The stub answers ``--version`` and otherwise prints one canned JSON
    report, so gate tests exercise the real subprocess boundary without
    depending on a downloaded detector.

    Returns
    -------
    pathlib.Path
        Path to the executable stub.
    """
    stub = directory / "nose"
    payload = STUB_REPORT if report is None else report
    stub.write_text(
        f"#!{sys.executable}\n"
        "import json, sys\n"
        "if '--version' in sys.argv:\n"
        f"    print({version!r})\n"
        "    raise SystemExit(0)\n"
        f"print(json.dumps({payload!r}))\n",
        encoding="utf-8",
    )
    stub.chmod(0o755)
    return stub


def gate_command(script: Path, *arguments: str) -> list[str]:
    """Build an isolated Python command for a copied gate script."""
    return [sys.executable, str(script), *arguments]


def gate_environment(**overrides: str) -> dict[str, str]:
    """Build a deterministic environment for gate subprocesses."""
    return {**os.environ, **overrides}


def run_gate_command(
    script: Path,
    *arguments: str,
    environment: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a copied gate command and capture its completed result."""
    return subprocess.run(  # ruff: ignore[subprocess-without-shell-equals-true] - fixed test interpreter and copied script
        gate_command(script, *arguments),
        cwd=script.parent.parent,
        env=gate_environment() if environment is None else environment,
        check=False,
        capture_output=True,
        text=True,
    )


def stub_settings() -> detector.NoseSettings:
    """Build the standard detector settings used by the gate tests.

    Vary individual fields at the call site with :func:`dataclasses.replace`
    rather than threading an override parameter per field through this factory.

    Returns
    -------
    detector.NoseSettings
        The pinned version, roots, channels, size floor, surface, ranking
        bound, and exclusions shared by the gate tests.
    """
    return detector.NoseSettings(
        version="0.20.0",
        roots=("falcon_pachinko",),
        mode="syntax,semantic,near",
        min_size=24,
        surface="all",
        top=30,
        exclude=(),
    )


def stub_runner(
    *, version: str = "nose 0.20.0", report: object = None
) -> detector.CommandRunner:
    """Build a command runner double answering version and query commands."""
    payload = STUB_REPORT if report is None else report

    def run(
        command: cabc.Sequence[str],
        _repository_root: Path,
        _environment: cabc.Mapping[str, str],
    ) -> str:
        if "--version" in command:
            return f"{version}\n"
        return json.dumps(payload)

    return run


def stub_detector_context(
    repository_root: Path,
    *,
    environment: cabc.Mapping[str, str] | None = None,
    command_runner: detector.CommandRunner | None = None,
    binary_discoverer: detector.BinaryDiscoverer | None = None,
) -> detector.DetectorContext:
    """Build an explicit detector context with no ambient process lookup."""
    return detector.DetectorContext(
        repository_root=repository_root,
        environment={"NOSE_BIN": "/stub/nose"} if environment is None else environment,
        binary_discoverer=(
            (lambda _root, _environment: None)
            if binary_discoverer is None
            else binary_discoverer
        ),
        command_runner=stub_runner() if command_runner is None else command_runner,
    )


def repository_detector_context(
    *, environment: cabc.Mapping[str, str] | None = None
) -> detector.DetectorContext:
    """Build a context for exercising the installed detector in this repo."""
    return detector.DetectorContext(
        repository_root=REPOSITORY_ROOT,
        environment=(gate_environment() if environment is None else environment),
        binary_discoverer=detector.discover_binary,
        command_runner=detector.run_command,
    )


__all__ = [
    "REPOSITORY_ROOT",
    "STUB_REPORT",
    "allowlist",
    "copied_gate_workspace",
    "detector",
    "gate",
    "gate_command",
    "gate_environment",
    "run_gate_command",
    "schema",
    "stub_detector_context",
    "stub_runner",
    "stub_settings",
    "write_stub_nose",
]
