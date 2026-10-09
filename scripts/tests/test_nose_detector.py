"""Tests for the pinned nose detector wrapper used by the duplication gate."""

import copy
import dataclasses as dc
import re
import textwrap
import typing as typ
from collections import abc as cabc
from pathlib import Path

import pytest
from duplication_gate_test_support import (
    STUB_REPORT,
    detector,
    stub_runner,
    stub_settings,
    write_stub_nose,
)
from syrupy import SnapshotAssertion


def _settings_body(
    *,
    version: str | None = '"0.20.0"',
    roots: str = '["falcon_pachinko"]',
    min_size: str = "24",
    surface: str | None = None,
) -> str:
    """Build a `[tool.nose]` table body from the supplied literal values."""
    lines = ["[tool.nose]"]
    if version is not None:
        lines.append(f"version = {version}")
    lines.extend((f"roots = {roots}", 'mode = "syntax"', f"min-size = {min_size}"))
    if surface is not None:
        lines.append(f"surface = {surface}")
    return "\n".join(lines) + "\n"


class TestLoadSettings:
    """`[tool.nose]` settings parsing."""

    def _write(self, tmp_path: Path, body: str) -> Path:
        """Write ``body`` to ``pyproject.toml`` under ``tmp_path``."""
        package = tmp_path / "falcon_pachinko"
        package.mkdir(exist_ok=True)
        (package / "sample.py").write_text("VALUE = 1\n", encoding="utf-8")
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(textwrap.dedent(body), encoding="utf-8")
        return pyproject

    def test_loads_the_repository_settings(self, tmp_path: Path) -> None:
        """A complete table produces validated settings."""
        pyproject = self._write(
            tmp_path,
            """\
            [tool.nose]
            version = "0.20.0"
            roots = ["falcon_pachinko"]
            mode = "syntax"
            min-size = 24
            surface = "all"
            top = 30
            exclude = ["**/generated/**"]
            """,
        )
        settings = detector.load_settings(pyproject)
        assert settings.roots == ("falcon_pachinko",), "Roots must round-trip in order."
        assert settings.exclude == ("**/generated/**",), (
            "Exclude globs must round-trip."
        )
        assert settings.top == 30, "The ranking bound must round-trip."

    def test_top_and_exclude_are_optional(self, tmp_path: Path) -> None:
        """Omitted optional keys fall back to nose's own view size."""
        pyproject = self._write(
            tmp_path,
            """\
            [tool.nose]
            version = "0.20.0"
            roots = ["falcon_pachinko"]
            mode = "syntax"
            min-size = 24
            """,
        )
        settings = detector.load_settings(pyproject)
        assert settings.top is None, "An omitted `top` must not bound the view."
        assert settings.surface == "all", "The gate defaults to the widened surface."

    def test_rejects_a_missing_root(self, tmp_path: Path) -> None:
        """A mistyped root cannot turn the configured scan into an empty pass."""
        pyproject = self._write(
            tmp_path,
            _settings_body(roots='["falcon_pachinko_typo"]'),
        )
        with pytest.raises(detector.GateConfigError, match="does not exist"):
            detector.load_settings(pyproject)

    def test_rejects_a_root_with_no_selected_python_files(self, tmp_path: Path) -> None:
        """A valid but excluded-only root is not accepted as a clean scan."""
        empty_root = tmp_path / "empty"
        empty_root.mkdir()
        (empty_root / "sample.py").write_text("VALUE = 1\n", encoding="utf-8")
        pyproject = self._write(
            tmp_path,
            """\
            [tool.nose]
            version = "0.20.0"
            roots = ["empty"]
            mode = "syntax"
            min-size = 24
            exclude = ["sample.py"]
            """,
        )
        with pytest.raises(
            detector.GateConfigError, match="selects no Python source files"
        ):
            detector.load_settings(pyproject)

    def test_rejects_a_root_with_only_gitignored_python_files(
        self, tmp_path: Path
    ) -> None:
        """A source ignored by nose cannot satisfy the configured scan scope."""
        ignored_root = tmp_path / "ignored"
        ignored_root.mkdir()
        (ignored_root / "sample.py").write_text("VALUE = 1\n", encoding="utf-8")
        (ignored_root / ".gitignore").write_text("*.py\n", encoding="utf-8")
        pyproject = self._write(
            tmp_path,
            _settings_body(roots='["ignored"]'),
        )

        with pytest.raises(
            detector.GateConfigError, match="selects no Python source files"
        ):
            detector.load_settings(pyproject)

    def test_honours_nested_gitignore_reinclusion(self, tmp_path: Path) -> None:
        """A nested positive rule can restore a source nose will scan."""
        root = tmp_path / "source"
        nested = root / "nested"
        nested.mkdir(parents=True)
        (root / ".gitignore").write_text("*.py\n", encoding="utf-8")
        (nested / ".gitignore").write_text("!selected.py\n", encoding="utf-8")
        (nested / "selected.py").write_text("VALUE = 1\n", encoding="utf-8")
        pyproject = self._write(
            tmp_path,
            _settings_body(roots='["source"]'),
        )

        settings = detector.load_settings(pyproject)

        assert settings.roots == ("source",), "The re-included source is in scope."

    def test_nested_gitignore_cannot_reinclude_a_source_in_ignored_directory(
        self, tmp_path: Path
    ) -> None:
        """Nested rules do not revive files below a parent-ignored directory."""
        root = tmp_path / "source"
        nested = root / "ignored"
        nested.mkdir(parents=True)
        (root / ".gitignore").write_text("ignored/\n", encoding="utf-8")
        (nested / ".gitignore").write_text("!selected.py\n", encoding="utf-8")
        (nested / "selected.py").write_text("VALUE = 1\n", encoding="utf-8")
        pyproject = self._write(
            tmp_path,
            _settings_body(roots='["source"]'),
        )

        with pytest.raises(
            detector.GateConfigError, match="selects no Python source files"
        ):
            detector.load_settings(pyproject)

    @pytest.mark.parametrize(
        ("body", "diagnostic"),
        [
            (
                _settings_body(version=None),
                "tool.nose.version must be a non-empty string",
            ),
            (
                _settings_body(roots='"falcon_pachinko"'),
                "tool.nose.roots must be an array of strings",
            ),
            (
                _settings_body(min_size="0"),
                "tool.nose.min-size must be a positive integer",
            ),
            (
                _settings_body(surface='"everything"'),
                "tool.nose.surface must be 'default' or 'all'",
            ),
        ],
        ids=["missing-version", "string-roots", "zero-min-size", "bad-surface"],
    )
    def test_rejects_malformed_settings(
        self, tmp_path: Path, body: str, diagnostic: str
    ) -> None:
        """Malformed settings raise a configuration error."""
        pyproject = self._write(tmp_path, body)
        with pytest.raises(detector.GateConfigError, match=re.escape(diagnostic)):
            detector.load_settings(pyproject)


class TestResolveBinary:
    """Discovery and version verification of the pinned binary."""

    def test_accepts_the_pinned_version(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A binary reporting the pinned version is accepted."""
        stub = write_stub_nose(tmp_path)
        monkeypatch.setenv("NOSE_BIN", str(stub))
        assert detector.resolve_binary(stub_settings(), runner=stub_runner()) == str(
            stub
        ), "The pinned binary must be returned unchanged."

    def test_resolves_a_relative_override_from_the_repository_root(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A relative NOSE_BIN is stable when callers run from another directory."""
        tools = tmp_path / "tools"
        tools.mkdir()
        stub = write_stub_nose(tools)
        monkeypatch.setattr(detector, "REPO_ROOT", tmp_path)
        monkeypatch.setenv("NOSE_BIN", "tools/nose")
        assert detector.resolve_binary(stub_settings(), runner=stub_runner()) == str(
            stub.resolve()
        ), "A relative override must resolve against the repository root."

    def test_rejects_a_version_mismatch(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A different installed version fails with a remediation hint."""
        stub = write_stub_nose(tmp_path, version="nose 0.19.0")
        monkeypatch.setenv("NOSE_BIN", str(stub))
        with pytest.raises(
            detector.GateExecutionError,
            match=r"reports 'nose 0\.19\.0'.*make install-nose",
        ):
            detector.resolve_binary(
                stub_settings(), runner=stub_runner(version="nose 0.19.0")
            )

    def test_reports_a_missing_binary(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A missing detector fails with the install remediation."""
        monkeypatch.delenv("NOSE_BIN", raising=False)
        monkeypatch.setattr(detector, "_discover_binary", lambda: None)
        with pytest.raises(detector.GateExecutionError, match="make install-nose"):
            detector.resolve_binary(stub_settings(), runner=stub_runner())


class TestBuildCommand:
    """Translation of gate settings into a nose query command."""

    def test_pins_every_configured_setting(self, snapshot: SnapshotAssertion) -> None:
        """The whole argument vector is pinned, in order, from the settings."""
        settings = dc.replace(
            stub_settings(),
            roots=("falcon_pachinko", "openai_test_types.py"),
            mode="semantic",
            min_size=40,
            surface="all",
            top=30,
            exclude=("**/generated/**", "**/_vendor/**"),
        )

        command = detector.build_command("nose", settings)

        assert command == snapshot, (
            "Every configured nose option must reach the detector in order."
        )

    def test_default_surface_omits_the_all_term(
        self, snapshot: SnapshotAssertion
    ) -> None:
        """The default surface leaves nose on its ranked dashboard."""
        settings = dc.replace(
            stub_settings(),
            roots=("falcon_pachinko",),
            mode="syntax",
            min_size=24,
            surface="default",
            top=None,
            exclude=(),
        )

        command = detector.build_command("nose", settings)

        assert command == snapshot, (
            "The default surface must omit an all-wide view and ranking bound."
        )


class TestRunDetector:
    """Report parsing and finding normalization."""

    def test_normalizes_a_stub_report(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A stub report becomes one ordered finding with both locations."""
        monkeypatch.setenv("NOSE_BIN", "/stub/nose")
        findings = detector.run_detector(stub_settings(), runner=stub_runner())
        assert len(findings) == 1, "The stub report contains one family."
        assert (
            findings[0].label
            == "falcon_pachinko/a.py:1-20 ~ falcon_pachinko/b.py:30-49"
        ), "Findings must report both spans."

    @pytest.mark.parametrize(
        ("families", "expected_values", "expected_labels"),
        [
            pytest.param(
                [
                    {
                        "witness": "copy-paste",
                        "value": 5.0,
                        "locations": [
                            {
                                "file": "falcon_pachinko/z.py",
                                "start": 1,
                                "end": 2,
                                "name": None,
                            },
                            {
                                "file": "falcon_pachinko/y.py",
                                "start": 1,
                                "end": 2,
                                "name": None,
                            },
                        ],
                    },
                    {
                        "witness": "exact",
                        "value": 9.0,
                        "locations": [
                            {
                                "file": "falcon_pachinko/a.py",
                                "start": 1,
                                "end": 2,
                                "name": "run",
                            },
                            {
                                "file": "falcon_pachinko/b.py",
                                "start": 1,
                                "end": 2,
                                "name": "run",
                            },
                        ],
                    },
                ],
                [9.0, 5.0],
                [
                    "falcon_pachinko/a.py:1-2 run ~ falcon_pachinko/b.py:1-2 run",
                    "falcon_pachinko/z.py:1-2 ~ falcon_pachinko/y.py:1-2",
                ],
                id="descending-value",
            ),
            pytest.param(
                [
                    {
                        "witness": "copy-paste",
                        "value": 5.0,
                        "locations": [
                            {
                                "file": "falcon_pachinko/z.py",
                                "start": 1,
                                "end": 2,
                                "name": None,
                            },
                            {
                                "file": "falcon_pachinko/y.py",
                                "start": 1,
                                "end": 2,
                                "name": None,
                            },
                        ],
                    },
                    {
                        "witness": "copy-paste",
                        "value": 5.0,
                        "locations": [
                            {
                                "file": "falcon_pachinko/b.py",
                                "start": 1,
                                "end": 2,
                                "name": None,
                            },
                            {
                                "file": "falcon_pachinko/a.py",
                                "start": 1,
                                "end": 2,
                                "name": None,
                            },
                        ],
                    },
                ],
                [5.0, 5.0],
                [
                    "falcon_pachinko/b.py:1-2 ~ falcon_pachinko/a.py:1-2",
                    "falcon_pachinko/z.py:1-2 ~ falcon_pachinko/y.py:1-2",
                ],
                id="location-label-tie-break",
            ),
        ],
    )
    def test_orders_findings(
        self,
        families: list[dict[str, object]],
        expected_values: list[float],
        expected_labels: list[str],
    ) -> None:
        """Findings sort by descending value, then by normalized location label."""
        findings = detector.normalize_findings({
            "schema_version": 9,
            "families": families,
        })

        assert [finding.value for finding in findings] == expected_values, (
            "Higher-value families must sort first."
        )
        assert [finding.label for finding in findings] == expected_labels, (
            "Equal values must order by normalized location label."
        )

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            pytest.param(7, 7.0, id="integer"),
            pytest.param(7.5, 7.5, id="float"),
        ],
    )
    def test_normalizes_numeric_values_to_float(
        self,
        value: float,
        expected: float,
    ) -> None:
        """Integer and floating-point family values both normalize to float."""
        report = copy.deepcopy(STUB_REPORT)
        typ.cast("dict[str, typ.Any]", report)["families"][0]["value"] = value
        findings = detector.normalize_findings(report)
        assert isinstance(findings[0].value, float), (
            "Normalization must coerce family values to float."
        )
        assert findings[0].value == expected, "Normalization must preserve the value."

    @pytest.mark.parametrize(
        ("mutate", "diagnostic"),
        [
            pytest.param(
                lambda report: report.__setitem__("schema_version", 8),
                "schema_version must be 9",
                id="unsupported-schema-version",
            ),
            pytest.param(
                lambda report: report.__setitem__("families", {}),
                "families must be an array",
                id="families-object",
            ),
            pytest.param(
                lambda report: report["families"][0].__setitem__("value", "high"),
                "value must be a number",
                id="string-value",
            ),
            pytest.param(
                lambda report: report["families"][0].update({"value": True}),
                "value must be a number",
                id="boolean-value",
            ),
            pytest.param(
                lambda report: report["families"][0].update({"value": float("nan")}),
                "value must be finite",
                id="non-finite-value",
            ),
            pytest.param(
                lambda report: report["families"][0].pop("value"),
                "value must be a number",
                id="missing-value",
            ),
            pytest.param(
                lambda report: report["families"][0].__setitem__(
                    "locations", "falcon_pachinko/a.py"
                ),
                "locations must be an array",
                id="string-locations",
            ),
            pytest.param(
                lambda report: report["families"][0].__setitem__(
                    "locations", b"falcon_pachinko/a.py"
                ),
                "locations must be an array",
                id="bytes-locations",
            ),
            pytest.param(
                lambda report: report["families"][0].__setitem__("locations", []),
                "locations must not be empty",
                id="empty-locations",
            ),
            pytest.param(
                lambda report: report["families"][0]["locations"].__setitem__(
                    0, "falcon_pachinko/a.py"
                ),
                "families[0].locations[0] must be a table",
                id="malformed-first-location",
            ),
            pytest.param(
                lambda report: report["families"][0]["locations"][0].__setitem__(
                    "start", 0
                ),
                "start must be a positive integer",
                id="zero-start",
            ),
            pytest.param(
                lambda report: report["families"][0]["locations"][0].__setitem__(
                    "end", 0
                ),
                "end must not precede start",
                id="inverted-span",
            ),
            pytest.param(
                lambda report: report["families"][0]["locations"][0].__setitem__(
                    "name", ""
                ),
                "name must be a non-empty string or null",
                id="empty-name",
            ),
            pytest.param(
                lambda report: report["families"][0]["locations"][0].__setitem__(
                    "file", "../outside.py"
                ),
                "repository-relative POSIX path",
                id="escaping-file-path",
            ),
            pytest.param(
                lambda report: report["families"][0]["locations"][0].__setitem__(
                    "file", r"falcon_pachinko\\outside.py"
                ),
                "repository-relative POSIX path",
                id="non-posix-file-path",
            ),
        ],
    )
    def test_rejects_malformed_reports(
        self,
        mutate: cabc.Callable[[dict[str, typ.Any]], None],
        diagnostic: str,
    ) -> None:
        """Schema violations fail at the detector boundary."""
        report = copy.deepcopy(STUB_REPORT)
        mutate(typ.cast("dict[str, typ.Any]", report))
        with pytest.raises(detector.GateConfigError, match=re.escape(diagnostic)):
            detector.normalize_findings(report)

    def test_rejects_unreadable_output(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Non-JSON detector output fails with an execution error."""
        monkeypatch.setenv("NOSE_BIN", "/stub/nose")

        def runner(command: cabc.Sequence[str]) -> str:
            return "nose 0.20.0\n" if "--version" in command else "not json"

        with pytest.raises(detector.GateExecutionError, match="not valid JSON"):
            detector.run_detector(stub_settings(), runner=runner)

    def test_run_command_reports_timeout(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A slow detector becomes an actionable execution error."""

        def timeout(*_args: object, **_kwargs: object) -> typ.NoReturn:
            raise detector.subprocess.TimeoutExpired(["nose", "query"], 120)

        monkeypatch.setattr(detector.subprocess, "run", timeout)

        with pytest.raises(
            detector.GateExecutionError, match="timed out after 120 seconds"
        ):
            detector._run_command(("nose", "query"))

    def test_run_command_reports_a_non_zero_exit(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A detector that fails carries its status and diagnostic out."""

        def failing(
            *_args: object, **_kwargs: object
        ) -> detector.subprocess.CompletedProcess[str]:
            return detector.subprocess.CompletedProcess(
                args=["nose", "query"], returncode=2, stdout="", stderr="bad query\n"
            )

        monkeypatch.setattr(detector.subprocess, "run", failing)

        with pytest.raises(
            detector.GateExecutionError,
            match=r"nose exited with status 2: bad query",
        ):
            detector._run_command(("nose", "query"))

    def test_run_command_reports_an_execution_failure(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An unrunnable detector points at the install remediation."""

        def unrunnable(*_args: object, **_kwargs: object) -> typ.NoReturn:
            raise OSError(13, "Permission denied")

        monkeypatch.setattr(detector.subprocess, "run", unrunnable)

        with pytest.raises(
            detector.GateExecutionError,
            match=r"cannot run nose: .*Permission denied.*make install-nose",
        ):
            detector._run_command(("nose", "query"))
