"""CLI validation of existing duplication exception containers."""

from pathlib import Path

import pytest
from duplication_gate_test_support import gate


@pytest.mark.parametrize(
    "scenario",
    [
        pytest.param(
            (
                'tool = "invalid"\n',
                "pyproject.tool must be a table with string keys",
            ),
            id="tool-is-scalar",
        ),
        pytest.param(
            (
                '[tool]\nduplication_gate = "invalid"\n',
                "pyproject.tool.duplication_gate must be a table with string keys",
            ),
            id="gate-is-scalar",
        ),
        pytest.param(
            (
                '[tool.duplication_gate]\nallow = "invalid"\n',
                "duplication_gate.allow must be an array",
            ),
            id="allow-is-scalar",
        ),
        pytest.param(
            (
                "[tool.duplication_gate]\nallow = {}\n",
                "duplication_gate.allow must be an array",
            ),
            id="allow-is-table",
        ),
    ],
)
def test_allow_rejects_malformed_containers_without_writing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    scenario: tuple[str, str],
) -> None:
    """Malformed but valid TOML exits with guidance and stays untouched."""
    contents, diagnostic = scenario
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(contents, encoding="utf-8")
    before = pyproject.read_bytes()
    monkeypatch.setattr(gate, "PYPROJECT", pyproject)

    with pytest.raises(SystemExit) as error:
        gate.allow(first="falcon_pachinko/a.py", reason="reviewed exception")

    assert error.value.code == 2, "Malformed TOML containers must return two."
    stderr = capsys.readouterr().err
    assert stderr == f"configuration error: {diagnostic}\n", (
        "Malformed containers must use actionable configuration diagnostics."
    )
    assert "Traceback" not in stderr, "The CLI must not leak a traceback."
    assert pyproject.read_bytes() == before, (
        "Rejected TOML containers must leave the manifest unchanged."
    )
