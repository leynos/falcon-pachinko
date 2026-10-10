"""Real-detector checks for configured scan scope."""

import dataclasses as dc
import json
import subprocess  # ruff: ignore[suspicious-subprocess-import] - runs the verified nose binary
import textwrap
from pathlib import Path

import pytest
from duplication_gate_test_support import (
    REPOSITORY_ROOT,
    copied_gate_workspace,
    detector,
    gate_environment,
    repository_detector_context,
    run_gate_command,
)


def _isolated_nose_environment(tmp_path: Path, binary: str) -> dict[str, str]:
    """Keep host Git ignore settings out of disposable scan fixtures."""
    environment = gate_environment(
        NOSE_BIN=binary,
        HOME=str(tmp_path / "home"),
        XDG_CONFIG_HOME=str(tmp_path / "xdg"),
        GIT_CONFIG_NOSYSTEM="1",
    )
    for key in tuple(environment):
        if key.startswith("GIT_CONFIG_") and key != "GIT_CONFIG_NOSYSTEM":
            del environment[key]
    (tmp_path / "home").mkdir(exist_ok=True)
    (tmp_path / "xdg").mkdir(exist_ok=True)
    return environment


def _ignored_only_workspace(
    tmp_path: Path, *, source_path: str, ignore_file: str | None
) -> Path:
    """Build a workspace whose configured Python root has no visible source."""
    workspace, _gate_script = copied_gate_workspace(tmp_path)
    package = workspace / "planted"
    package.mkdir()
    source = package / source_path
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text("VALUE = 1\n", encoding="utf-8")
    if ignore_file is not None:
        (package / ignore_file).write_text("*.py\n", encoding="utf-8")
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


@pytest.mark.parametrize(
    ("source_path", "ignore_file"),
    [
        pytest.param("sample.py", ".gitignore", id="gitignore-only"),
        pytest.param("sample.py", ".ignore", id="ignore-only"),
        pytest.param(".hidden/sample.py", None, id="hidden-only"),
    ],
)
def test_ignored_only_root_is_rejected_by_real_detector_gate(
    tmp_path: Path, source_path: str, ignore_file: str | None
) -> None:
    """Nose's effective empty selection cannot masquerade as a clean scan."""
    workspace = _ignored_only_workspace(
        tmp_path, source_path=source_path, ignore_file=ignore_file
    )
    repository_settings = detector.load_settings(REPOSITORY_ROOT / "pyproject.toml")
    binary = detector.resolve_binary(
        repository_settings, context=repository_detector_context()
    )
    settings = dc.replace(repository_settings, roots=("planted",))
    query = detector.build_command(binary, settings)

    raw_report = subprocess.run(  # ruff: ignore[subprocess-without-shell-equals-true] - configured query uses the verified pinned nose binary
        query,
        cwd=workspace,
        env=_isolated_nose_environment(tmp_path, binary),
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
        environment=_isolated_nose_environment(tmp_path, binary),
    )

    assert gate_result.returncode == 2, (
        "An ignored-only root must fail configuration.\n"
        f"{gate_result.stdout}{gate_result.stderr}"
    )
    assert "nose selected no supported Python source files under planted" in (
        gate_result.stderr
    ), "The diagnostic must explain that the configured root is empty."
    assert "global Git ignore rules" in gate_result.stderr, (
        "The diagnostic must identify ignore sources that affect selection."
    )
    assert "Traceback" not in gate_result.stderr, "The gate must fail cleanly."
