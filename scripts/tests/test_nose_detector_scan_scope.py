"""Real-detector checks for configured scan scope."""

import dataclasses as dc
import json
import subprocess  # ruff: ignore[suspicious-subprocess-import] - runs the verified nose binary
import textwrap
from pathlib import Path

from duplication_gate_test_support import (
    REPOSITORY_ROOT,
    copied_gate_workspace,
    detector,
    gate_environment,
    repository_detector_context,
    run_gate_command,
)


def _ignored_only_workspace(tmp_path: Path) -> Path:
    """Build a workspace whose configured Python root is entirely ignored."""
    workspace, _gate_script = copied_gate_workspace(tmp_path)
    package = workspace / "planted"
    package.mkdir()
    (package / "sample.py").write_text("VALUE = 1\n", encoding="utf-8")
    (package / ".gitignore").write_text("*.py\n", encoding="utf-8")
    (workspace / "pyproject.toml").write_text(
        textwrap.dedent(
            """\
            [project]
            name = "gate-test"
            version = "0"

            [tool.nose]
            version = "0.20.0"
            roots = ["planted"]
            mode = "syntax,semantic,near"
            min-size = 24
            surface = "all"
            top = 30
            """
        ),
        encoding="utf-8",
    )
    return workspace


def test_ignored_only_root_is_rejected_by_real_detector_gate(tmp_path: Path) -> None:
    """A real nose empty-walk report cannot masquerade as a clean scan."""
    workspace = _ignored_only_workspace(tmp_path)
    repository_settings = detector.load_settings(REPOSITORY_ROOT / "pyproject.toml")
    binary = detector.resolve_binary(
        repository_settings, context=repository_detector_context()
    )
    settings = dc.replace(repository_settings, roots=("planted",))
    query = detector.build_command(binary, settings)

    raw_report = subprocess.run(  # ruff: ignore[subprocess-without-shell-equals-true] - configured query uses the verified pinned nose binary
        query,
        cwd=workspace,
        env=gate_environment(NOSE_BIN=binary),
        check=False,
        capture_output=True,
        text=True,
    )
    assert raw_report.returncode == 0, raw_report.stderr
    assert json.loads(raw_report.stdout)["families"] == [], (
        "The detector emits an empty report when every source is ignored."
    )
    assert "no supported source files found under: planted" in raw_report.stderr, (
        "The real detector must expose its empty effective scan diagnostic."
    )

    gate_result = run_gate_command(
        workspace / "scripts" / "duplication_gate.py",
        "check",
        environment=gate_environment(NOSE_BIN=binary),
    )

    assert gate_result.returncode == 2, (
        "An ignored-only root must fail configuration.\n"
        f"{gate_result.stdout}{gate_result.stderr}"
    )
    assert "selects no Python source files" in gate_result.stderr, (
        "The diagnostic must explain that the configured root is empty."
    )
    assert "Traceback" not in gate_result.stderr, "The gate must fail cleanly."
