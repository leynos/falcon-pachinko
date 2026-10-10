"""Runtime-context composition at the duplication gate CLI boundary."""

import pytest
from duplication_gate_test_support import detector, gate


def test_gate_cli_composes_an_explicit_detector_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The CLI supplies process inputs and adapters to detector execution."""
    selected_settings = detector.NoseSettings(
        version="0.20.0",
        roots=("falcon_pachinko",),
        mode="syntax,semantic,near",
        min_size=24,
        surface="all",
        top=30,
        exclude=(),
    )
    captured: list[tuple[detector.NoseSettings, detector.DetectorContext]] = []

    def run(
        selected: detector.NoseSettings, *, context: detector.DetectorContext
    ) -> list[detector.Finding]:
        captured.append((selected, context))
        return []

    monkeypatch.setattr(gate, "load_settings", lambda _path: selected_settings)
    monkeypatch.setattr(gate, "run_detector", run)
    monkeypatch.setenv("NOSE_BIN", "custom/nose")

    assert gate.detect_findings() == [], "A clean detector report remains empty."
    assert len(captured) == 1, "One detector call should receive the context."
    selected, context = captured[0]
    assert selected is selected_settings, "The configured settings reach the runner."
    assert context.repository_root == gate.REPO_ROOT, (
        "The CLI supplies the explicit repository root."
    )
    assert context.environment["NOSE_BIN"] == "custom/nose", (
        "The CLI supplies its environment snapshot."
    )
    assert context.binary_discoverer is detector.discover_binary, (
        "Binary discovery is supplied as an adapter."
    )
    assert context.command_runner is detector.run_command, (
        "Version and query commands use the supplied runner."
    )
