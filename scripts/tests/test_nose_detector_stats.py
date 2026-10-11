"""Per-root Nose statistics and source-selection validation tests."""

import dataclasses as dc
from collections import abc as cabc
from pathlib import Path

import pytest
from duplication_gate_test_support import (
    detector,
    stub_detector_context,
    stub_runner,
    stub_settings,
)


def test_stats_command_selects_one_root_and_requests_json() -> None:
    """Root checks stay independent while the duplication query stays combined."""
    assert detector.build_stats_command("nose", "falcon_pachinko") == [
        "nose",
        "stats",
        "falcon_pachinko",
        "--format",
        "json",
    ], "Each effective-source check must ask Nose for one root's JSON stats."


def test_each_root_is_checked_before_one_combined_query(tmp_path: Path) -> None:
    """Each root is inventoried before one combined cross-root query."""
    calls: list[tuple[list[str], Path, cabc.Mapping[str, str]]] = []
    canned_runner = stub_runner()

    def runner(
        command: cabc.Sequence[str],
        repository_root: Path,
        environment: cabc.Mapping[str, str],
    ) -> str:
        calls.append((list(command), repository_root, dict(environment)))
        return canned_runner(command, repository_root, environment)

    context = stub_detector_context(
        tmp_path,
        environment={"NOSE_BIN": "/fixture/nose", "PATH": "/fixture/bin"},
        command_runner=runner,
    )

    settings = dc.replace(stub_settings(), roots=("falcon_pachinko", "other"))
    detector.run_detector(settings, context=context)

    commands = [call[0] for call in calls]
    command_kinds = [
        command[1] if len(command) > 1 else command[0] for command in commands
    ]
    assert command_kinds == ["--version", "stats", "stats", "query"], (
        "Version and per-root stats must precede exactly one query."
    )
    stats_commands = [
        command for command in commands if len(command) > 1 and command[1] == "stats"
    ]
    assert [command[2] for command in stats_commands] == [
        "falcon_pachinko",
        "other",
    ], "Each configured root must be inventoried independently."
    query_commands = [
        command for command in commands if len(command) > 1 and command[1] == "query"
    ]
    assert len(query_commands) == 1, "Cross-root duplicates require one combined query."
    assert query_commands[0][query_commands[0].index("--root") + 1] == (
        "falcon_pachinko"
    ), "The combined query must retain the first configured root."
    assert query_commands[0][query_commands[0].index("--root", 3) + 1] == "other", (
        "The combined query must retain every configured root."
    )
    assert all(call[1] == tmp_path for call in calls), (
        "All commands must use the injected repository root."
    )
    assert all(call[2] == context.environment for call in calls), (
        "All commands must use the injected environment."
    )


def test_empty_python_stats_fail_before_the_combined_query(tmp_path: Path) -> None:
    """Visible non-Python files cannot make an empty root pass."""
    calls: list[list[str]] = []

    def runner(
        command: cabc.Sequence[str],
        _repository_root: Path,
        _environment: cabc.Mapping[str, str],
    ) -> str:
        calls.append(list(command))
        if "--version" in command:
            return "nose 0.20.0\n"
        if len(command) > 1 and command[1] == "stats":
            source_language = "python" if command[2] == "visible" else "javascript"
            return (
                f'{{"files":1,"per_lang":[{{"lang":"{source_language}","files":1}}]}}'
            )
        return '{"families": []}'

    context = stub_detector_context(tmp_path, command_runner=runner)

    with pytest.raises(
        detector.GateConfigError,
        match="nose selected no Python source files under other",
    ):
        detector.run_detector(
            dc.replace(stub_settings(), roots=("visible", "other")), context=context
        )

    assert not any(command[1] == "query" for command in calls if len(command) > 1), (
        "The combined query must not run after an empty configured root."
    )


@pytest.mark.parametrize(
    ("stats_report", "diagnostic"),
    [
        ('{"per_lang": "python"}', "per_lang must be an array"),
        (
            '{"per_lang":[{"lang":"python","files":true}]}',
            "files must be a non-negative integer",
        ),
    ],
    ids=["invalid-language-list", "invalid-python-file-count"],
)
def test_malformed_stats_cannot_be_treated_as_valid(
    tmp_path: Path, stats_report: str, diagnostic: str
) -> None:
    """Malformed per-root inventory data fails closed."""

    def runner(
        command: cabc.Sequence[str],
        _repository_root: Path,
        _environment: cabc.Mapping[str, str],
    ) -> str:
        if "--version" in command:
            return "nose 0.20.0\n"
        return stats_report

    context = stub_detector_context(tmp_path, command_runner=runner)

    with pytest.raises(detector.GateConfigError, match=diagnostic):
        detector.run_detector(stub_settings(), context=context)
