"""Tests for the pinned nose detector wrapper used by the duplication gate."""

import copy
import dataclasses as dc
import inspect
import re
import textwrap
import typing as typ
from collections import abc as cabc
from pathlib import Path

import pytest
from duplication_gate_test_support import (
    STUB_REPORT,
    detector,
    stub_detector_context,
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

    def test_gitignore_exclusion_matches_python_files_at_nested_depth(
        self, tmp_path: Path
    ) -> None:
        """An unanchored exclude cannot leave nose with an empty effective root."""
        root = tmp_path / "source"
        nested = root / "pkg"
        nested.mkdir(parents=True)
        (nested / "sample.py").write_text("VALUE = 1\n", encoding="utf-8")
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(
            textwrap.dedent(
                """\
                [tool.nose]
                version = "0.20.0"
                roots = ["source"]
                mode = "syntax"
                min-size = 24
                exclude = ["*.py"]
                """
            ),
            encoding="utf-8",
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


class TestSourceRelativePath:
    """Normalization of source candidates against one configured root."""

    def test_returns_a_root_relative_posix_path(self, tmp_path: Path) -> None:
        """Nested source candidates retain their stable relative location."""
        root = tmp_path / "source"
        nested = root / "nested"
        nested.mkdir(parents=True)
        source = nested / "sample.py"
        source.write_text("VALUE = 1\n", encoding="utf-8")

        relative_path = detector._source_relative_path(
            source,
            resolved_root=root.resolve(),
            is_file_root=False,
        )

        assert relative_path == "nested/sample.py", (
            "Directory scans must use POSIX paths relative to their configured root."
        )

    def test_accepts_only_the_file_selected_as_a_file_root(
        self, tmp_path: Path
    ) -> None:
        """A file root cannot admit a neighbouring source candidate."""
        selected = tmp_path / "selected.py"
        neighbour = tmp_path / "neighbour.py"
        selected.write_text("VALUE = 1\n", encoding="utf-8")
        neighbour.write_text("VALUE = 2\n", encoding="utf-8")

        selected_relative_path = detector._source_relative_path(
            selected,
            resolved_root=selected.resolve(),
            is_file_root=True,
        )
        relative_path = detector._source_relative_path(
            neighbour,
            resolved_root=selected.resolve(),
            is_file_root=True,
        )

        assert selected_relative_path == selected.name, (
            "A selected file root must retain its repository-relative filename."
        )
        assert relative_path is None, "A file root must select one exact file."


class TestResolveBinary:
    """Discovery and version verification of the pinned binary."""

    def test_binary_and_query_boundaries_require_an_explicit_context(self) -> None:
        """Core detector operations cannot read process dependencies implicitly."""
        assert (
            inspect.signature(detector.resolve_binary).parameters["context"].default
            is inspect.Parameter.empty
        ), "Binary resolution must receive its environment and adapters."
        assert (
            inspect.signature(detector.run_detector).parameters["context"].default
            is inspect.Parameter.empty
        ), "Detector execution must receive its environment and adapters."

    def test_accepts_the_pinned_version(self, tmp_path: Path) -> None:
        """A binary reporting the pinned version is accepted."""
        stub = write_stub_nose(tmp_path)
        context = stub_detector_context(tmp_path, environment={"NOSE_BIN": str(stub)})
        assert detector.resolve_binary(stub_settings(), context=context) == str(stub), (
            "The pinned binary must be returned unchanged."
        )

    def test_resolves_a_relative_override_from_the_repository_root(
        self, tmp_path: Path
    ) -> None:
        """A relative NOSE_BIN is stable when callers run from another directory."""
        tools = tmp_path / "tools"
        tools.mkdir()
        stub = write_stub_nose(tools)
        context = stub_detector_context(
            tmp_path, environment={"NOSE_BIN": "tools/nose"}
        )
        assert detector.resolve_binary(stub_settings(), context=context) == str(
            stub.resolve()
        ), "A relative override must resolve against the supplied repository root."

    def test_rejects_a_version_mismatch(self, tmp_path: Path) -> None:
        """A different installed version fails with a remediation hint."""
        stub = write_stub_nose(tmp_path, version="nose 0.19.0")
        context = stub_detector_context(
            tmp_path,
            environment={"NOSE_BIN": str(stub)},
            command_runner=stub_runner(version="nose 0.19.0"),
        )
        with pytest.raises(
            detector.GateExecutionError,
            match=r"reports 'nose 0\.19\.0'.*make install-nose",
        ):
            detector.resolve_binary(stub_settings(), context=context)

    def test_reports_a_missing_binary(self, tmp_path: Path) -> None:
        """A missing detector fails with the install remediation."""
        context = stub_detector_context(tmp_path, environment={})
        with pytest.raises(detector.GateExecutionError, match="make install-nose"):
            detector.resolve_binary(stub_settings(), context=context)

    def test_discovery_receives_only_the_supplied_root_and_environment(
        self, tmp_path: Path
    ) -> None:
        """Binary discovery uses the context instead of ambient PATH or globals."""
        discovered = tmp_path / "tools" / "nose"
        calls: list[tuple[Path, cabc.Mapping[str, str]]] = []

        def discover(root: Path, environment: cabc.Mapping[str, str]) -> str:
            calls.append((root, environment))
            return str(discovered)

        context = stub_detector_context(
            tmp_path,
            environment={"PATH": "/controlled/path"},
            binary_discoverer=discover,
        )

        assert detector.resolve_binary(stub_settings(), context=context) == str(
            discovered
        ), "An injected discoverer supplies the selected binary."
        assert calls == [(tmp_path, {"PATH": "/controlled/path"})], (
            "Discovery must receive only the explicit root and environment."
        )


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

    def test_normalizes_a_stub_report(self, tmp_path: Path) -> None:
        """A stub report becomes one ordered finding with both locations."""
        context = stub_detector_context(tmp_path)
        findings = detector.run_detector(stub_settings(), context=context)
        assert len(findings) == 1, "The stub report contains one family."
        assert (
            findings[0].label
            == "falcon_pachinko/a.py:1-20 ~ falcon_pachinko/b.py:30-49"
        ), "Findings must report both spans."

    def test_runner_receives_the_context_for_version_and_query(
        self, tmp_path: Path
    ) -> None:
        """The injected root and environment reach both subprocess commands."""
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

        detector.run_detector(stub_settings(), context=context)

        assert [call[0][-1] for call in calls] == ["--version", "json"], (
            "The context must run version verification before one JSON query."
        )
        assert all(call[1] == tmp_path for call in calls), (
            "Both commands must use the injected repository root."
        )
        assert all(call[2] == context.environment for call in calls), (
            "Both commands must use the injected environment."
        )

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

    def test_rejects_unreadable_output(self, tmp_path: Path) -> None:
        """Non-JSON detector output fails with an execution error."""

        def runner(
            command: cabc.Sequence[str],
            _repository_root: Path,
            _environment: cabc.Mapping[str, str],
        ) -> str:
            return "nose 0.20.0\n" if "--version" in command else "not json"

        context = stub_detector_context(tmp_path, command_runner=runner)
        with pytest.raises(detector.GateExecutionError, match="not valid JSON"):
            detector.run_detector(stub_settings(), context=context)

    def test_run_command_reports_timeout(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A slow detector becomes an actionable execution error."""

        def timeout(*_args: object, **_kwargs: object) -> typ.NoReturn:
            raise detector.subprocess.TimeoutExpired(["nose", "query"], 120)

        monkeypatch.setattr(detector.subprocess, "run", timeout)

        with pytest.raises(
            detector.GateExecutionError, match="timed out after 120 seconds"
        ):
            detector.run_command(("nose", "query"), tmp_path, {})

    def test_run_command_reports_a_non_zero_exit(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
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
            detector.run_command(("nose", "query"), tmp_path, {})

    def test_run_command_reports_an_execution_failure(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An unrunnable detector points at the install remediation."""

        def unrunnable(*_args: object, **_kwargs: object) -> typ.NoReturn:
            raise OSError(13, "Permission denied")

        monkeypatch.setattr(detector.subprocess, "run", unrunnable)

        with pytest.raises(
            detector.GateExecutionError,
            match=r"cannot run nose: .*Permission denied.*make install-nose",
        ):
            detector.run_command(("nose", "query"), tmp_path, {})
