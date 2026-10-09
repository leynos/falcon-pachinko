"""Command-line behaviour tests for the duplication gate."""

import dataclasses as dc
import json
import subprocess  # ruff: ignore[suspicious-subprocess-import] - tests exercise copied gate and Make commands
import sys
import textwrap
import tomllib
from pathlib import Path

import pytest
from duplication_gate_test_support import (
    REPOSITORY_ROOT,
    allowlist,
    copied_gate_workspace,
    detector,
    gate,
    gate_environment,
    run_gate_command,
    write_stub_nose,
)
from syrupy import SnapshotAssertion


def _finding() -> detector.Finding:
    """Build a representative blocking finding."""
    return detector.Finding(
        witness="copy-paste",
        value=22.1,
        locations=(
            detector.Location(file="falcon_pachinko/a.py", start=1, end=20, name=None),
            detector.Location(
                file="falcon_pachinko/b.py", start=30, end=49, name="beta"
            ),
        ),
    )


class TestGateCommands:
    """CLI orchestration and real workflow contracts."""

    def test_check_reports_blocking_findings(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        snapshot: SnapshotAssertion,
    ) -> None:
        """The check command emits the blocking report and status one."""
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(gate, "load_allowlist", lambda _path: ())
        monkeypatch.setattr(gate, "detect_findings", lambda: [_finding()])
        with pytest.raises(SystemExit) as error:
            gate.check()
        assert error.value.code == 1, "Blocking findings must return status one."
        output = capsys.readouterr().out
        assert (output.splitlines(), output.endswith("\n")) == snapshot, (
            "Blocking report must remain actionable and deterministic."
        )

    def test_check_reports_stale_entries(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Unmatched entries are reported without claiming duplication is gone."""
        monkeypatch.chdir(tmp_path)
        entry = allowlist.AllowEntry(
            keys=("falcon_pachinko/gone.py",), reason="resolved"
        )
        monkeypatch.setattr(gate, "load_allowlist", lambda _path: (entry,))
        monkeypatch.setattr(gate, "detect_findings", lambda: [])
        gate.check()
        assert capsys.readouterr().out == (
            "stale allow entry (falcon_pachinko/gone.py): unmatched in this "
            "capped scan; inspect the source or widen the relevant scan before "
            "removing it\n"
            "duplication gate passed\n"
        ), "Stale entries must be reported alongside a passing gate."

    @pytest.mark.parametrize(
        "error",
        [
            pytest.param(OSError("unreadable configuration"), id="allowlist-io"),
            pytest.param(OSError("detector executable unavailable"), id="detector-io"),
        ],
    )
    def test_check_inputs_wrap_environment_failures(self, error: Exception) -> None:
        """Injected reader and detector failures become explicit gate errors."""
        if str(error).startswith("unreadable"):

            def reader(_path: object) -> tuple[allowlist.AllowEntry, ...]:
                raise error

            def detect() -> list[detector.Finding]:
                return []

        else:

            def reader(_path: object) -> tuple[allowlist.AllowEntry, ...]:
                return ()

            def detect() -> list[detector.Finding]:
                raise error

        with pytest.raises(gate.GateExecutionError, match=str(error)):
            gate._check_inputs(allowlist_reader=reader, detector=detect)

    def test_read_allowlist_returns_the_reader_result(self) -> None:
        """A successful reader result reaches the caller unchanged."""
        entry = allowlist.AllowEntry(keys=("falcon_pachinko/a.py",), reason="reviewed")

        def reader(_path: object) -> tuple[allowlist.AllowEntry, ...]:
            return (entry,)

        assert gate._read_allowlist(reader) == (entry,), (
            "The reader's entries must pass through unchanged."
        )

    def test_detect_findings_returns_the_detector_result(self) -> None:
        """A successful detector result reaches the caller unchanged."""
        finding = detector.Finding(
            witness="copy-paste",
            value=9.0,
            locations=(
                detector.Location(
                    file="falcon_pachinko/a.py", start=1, end=2, name=None
                ),
                detector.Location(
                    file="falcon_pachinko/b.py", start=1, end=2, name=None
                ),
            ),
        )

        def detect() -> list[detector.Finding]:
            return [finding]

        assert gate._detect_findings(detect) == [finding], (
            "The detector's findings must pass through unchanged."
        )

    @pytest.mark.parametrize(
        ("error", "expected_type", "expected_message"),
        [
            pytest.param(
                OSError("unreadable configuration"),
                gate.GateExecutionError,
                "cannot load duplication allowlist: unreadable configuration",
                id="os-error",
            ),
            pytest.param(
                tomllib.TOMLDecodeError("bad table", "", 0),
                gate.GateExecutionError,
                "cannot load duplication allowlist: bad table (at end of document)",
                id="toml-error",
            ),
        ],
    )
    def test_read_allowlist_translates_environment_failures(
        self,
        error: Exception,
        expected_type: type[Exception],
        expected_message: str,
    ) -> None:
        """Unreadable configuration becomes an explicit execution error."""

        def reader(_path: object) -> tuple[allowlist.AllowEntry, ...]:
            raise error

        with pytest.raises(expected_type) as raised:
            gate._read_allowlist(reader)

        assert str(raised.value) == expected_message, "Diagnostic must name the cause."
        assert raised.value.__cause__ is error, "The original error must be the cause."

    @pytest.mark.parametrize(
        ("error", "expected_type", "expected_message"),
        [
            pytest.param(
                OSError("detector executable unavailable"),
                gate.GateExecutionError,
                "nose detector failed: detector executable unavailable",
                id="os-error",
            ),
            pytest.param(
                TypeError("families must be an array"),
                gate.GateConfigError,
                "families must be an array",
                id="type-error",
            ),
            pytest.param(
                ValueError("value must be a number"),
                gate.GateConfigError,
                "value must be a number",
                id="value-error",
            ),
        ],
    )
    def test_detect_findings_translates_detector_failures(
        self,
        error: Exception,
        expected_type: type[Exception],
        expected_message: str,
    ) -> None:
        """Execution failures and schema violations use distinct gate errors."""

        def detect() -> list[detector.Finding]:
            raise error

        with pytest.raises(expected_type) as raised:
            gate._detect_findings(detect)

        assert str(raised.value) == expected_message, "Diagnostic must name the cause."
        assert raised.value.__cause__ is error, "The original error must be the cause."

    def test_detect_findings_does_not_wrap_runtime_errors(self) -> None:
        """Programming faults from a detector are not reclassified as I/O failures."""
        error = RuntimeError("detector runtime failed")

        def detect() -> list[detector.Finding]:
            raise error

        with pytest.raises(RuntimeError) as raised:
            gate._detect_findings(detect)

        assert raised.value is error, "Runtime errors must propagate unchanged."

    def test_read_allowlist_passes_configuration_errors_through(self) -> None:
        """An allowlist configuration error is not rewrapped."""
        error = gate.GateConfigError("duplication_gate.allow[0] requires a reason")

        def reader(_path: object) -> tuple[allowlist.AllowEntry, ...]:
            raise error

        with pytest.raises(gate.GateConfigError) as raised:
            gate._read_allowlist(reader)

        assert raised.value is error, "The original configuration error must propagate."

    def test_detect_findings_passes_configuration_errors_through(self) -> None:
        """A detector configuration error is not rewrapped."""
        error = gate.GateConfigError("nose 0.19.0 is installed but 0.20.0 is pinned")

        def detect() -> list[detector.Finding]:
            raise error

        with pytest.raises(gate.GateConfigError) as raised:
            gate._detect_findings(detect)

        assert raised.value is error, "The original configuration error must propagate."

    def test_check_reports_detector_schema_errors(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Malformed detector reports exit cleanly instead of showing a traceback."""
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(gate, "load_allowlist", lambda _path: ())

        def raise_schema_error() -> list[detector.Finding]:
            msg = "nose report families must be an array"
            raise TypeError(msg)

        monkeypatch.setattr(gate, "detect_findings", raise_schema_error)
        with pytest.raises(SystemExit) as error:
            gate.check()

        assert error.value.code == 2, "Malformed detector reports must return two."
        assert capsys.readouterr().err == (
            "configuration error: nose report families must be an array\n"
        ), "Schema errors must use the configuration diagnostic."
        assert Path.cwd() == tmp_path, (
            "The check command must not change its caller's working directory."
        )

    def test_check_reports_a_version_mismatch(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """An unpinned detector fails the gate with a remediation message."""
        workspace = tmp_path
        stub = write_stub_nose(workspace, version="nose 0.19.0")
        monkeypatch.setenv("NOSE_BIN", str(stub))
        monkeypatch.chdir(workspace)
        monkeypatch.setattr(gate, "load_allowlist", lambda _path: ())
        with pytest.raises(SystemExit) as error:
            gate.check()
        assert error.value.code == 2, "A version mismatch must return two."
        assert "make install-nose" in capsys.readouterr().err, (
            "The mismatch diagnostic must name the install remediation."
        )

    def test_allow_reports_malformed_existing_entries(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Malformed existing allows exit cleanly instead of showing a traceback."""
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(
            '[[tool.duplication_gate.allow]]\nunit = "falcon_pachinko/a.py"\n',
            encoding="utf-8",
        )
        monkeypatch.setattr(gate, "PYPROJECT", pyproject)

        with pytest.raises(SystemExit) as error:
            gate.allow(first="falcon_pachinko/b.py", reason="reviewed exception")

        assert error.value.code == 2, "Malformed existing allows must return two."
        assert capsys.readouterr().err == (
            "configuration error: duplication_gate.allow[0] "
            "requires a non-empty reason\n"
        ), "Malformed allows must use the configuration diagnostic."

    def test_allow_reports_write_failures(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """A filesystem failure while recording an allow exits cleanly."""
        write_error = OSError("read-only filesystem")

        def fail_write(*_args: object, **_kwargs: object) -> None:
            raise write_error

        monkeypatch.setattr(gate, "append_allow_entry", fail_write)

        with pytest.raises(SystemExit) as exit_error:
            gate.allow(first="falcon_pachinko/a.py", reason="reviewed exception")

        assert exit_error.value.code == 2, "Write failures must return two."
        assert capsys.readouterr().err == (
            "configuration error: read-only filesystem\n"
        ), "Write failures must use the configuration diagnostic."

    def test_allow_rejects_malformed_keys(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """An absolute key is refused before anything is written."""
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text("[project]\nname = 'x'\n", encoding="utf-8")
        monkeypatch.setattr(gate, "PYPROJECT", pyproject)

        with pytest.raises(SystemExit) as error:
            gate.allow(first="/falcon_pachinko/a.py", reason="reviewed exception")

        assert error.value.code == 2, "Malformed keys must return two."
        assert "repository-relative" in capsys.readouterr().err, (
            "The diagnostic must explain the key requirement."
        )
        assert "duplication_gate" not in pyproject.read_text(encoding="utf-8"), (
            "A rejected key must not be recorded."
        )

    @pytest.mark.parametrize(
        ("second", "expected_keys"),
        [
            pytest.param(None, ("falcon_pachinko/a.py",), id="unit"),
            pytest.param(
                ["falcon_pachinko/b.py::beta"],
                ("falcon_pachinko/a.py", "falcon_pachinko/b.py::beta"),
                id="members",
            ),
            pytest.param(
                ["falcon_pachinko/b.py", "falcon_pachinko/c.py"],
                (
                    "falcon_pachinko/a.py",
                    "falcon_pachinko/b.py",
                    "falcon_pachinko/c.py",
                ),
                id="three-members",
            ),
        ],
    )
    def test_allow_cli_round_trips_unit_and_members(
        self,
        tmp_path: Path,
        second: list[str] | None,
        expected_keys: tuple[str, ...],
    ) -> None:
        """The real allow CLI records both supported exception forms."""
        _workspace, script = copied_gate_workspace(tmp_path)
        arguments = ["allow", "--first", "falcon_pachinko/a.py"]
        for key in second or ():
            arguments.extend(("--second", key))
        arguments.extend(("--reason", "reviewed exception"))

        result = run_gate_command(script, *arguments)
        assert result.returncode == 0, result.stderr
        entries = allowlist.load_allowlist(script.parent.parent / "pyproject.toml")
        assert entries[0].keys == expected_keys, "CLI must retain its requested keys."
        assert entries[0].reason == "reviewed exception", (
            "CLI must retain the supplied reason."
        )

    def test_check_cli_passes_with_a_stub_detector(self, tmp_path: Path) -> None:
        """The gate exits zero through its real CLI when every family is allowed."""
        workspace, script = copied_gate_workspace(tmp_path)
        stub = write_stub_nose(tmp_path)
        (workspace / "pyproject.toml").write_text(
            textwrap.dedent(
                """\
                [tool.nose]
                version = "0.20.0"
                roots = ["falcon_pachinko"]
                mode = "syntax"
                min-size = 24

                [[tool.duplication_gate.allow]]
                members = ["falcon_pachinko/a.py", "falcon_pachinko/b.py"]
                reason = "parallel wire contracts"
                """
            ),
            encoding="utf-8",
        )
        result = run_gate_command(
            script,
            "check",
            environment=gate_environment(NOSE_BIN=str(stub)),
        )
        assert result.returncode == 0, result.stderr
        assert "duplication gate passed" in result.stdout, (
            "An allowed family must leave the gate passing."
        )

    def test_real_check_cli_passes(self) -> None:
        """The checked-in gate runs successfully through its real CLI boundary."""
        settings = detector.load_settings(REPOSITORY_ROOT / "pyproject.toml")
        try:
            detector.resolve_binary(settings)
        except detector.GateExecutionError as error:  # pragma: no cover
            pytest.skip(str(error))
        result = subprocess.run(  # ruff: ignore[subprocess-without-shell-equals-true] - fixed repository gate command
            [
                sys.executable,
                str(REPOSITORY_ROOT / "scripts" / "duplication_gate.py"),
                "check",
            ],
            cwd=REPOSITORY_ROOT,
            env=gate_environment(),
            check=False,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr
        assert "duplication gate passed" in result.stdout, (
            "Real check invocation must report its successful gate result."
        )

    def test_reports_a_planted_verbatim_copy(self, tmp_path: Path) -> None:
        """The pinned detector reports a planted copy through normalization."""
        settings = detector.load_settings(REPOSITORY_ROOT / "pyproject.toml")
        try:
            binary = detector.resolve_binary(settings)
        except detector.GateExecutionError as error:  # pragma: no cover
            pytest.skip(str(error))
        body = textwrap.dedent(
            """\
            def NAME(items):
                total = 0.0
                for item in items:
                    price = item["price"] * item["quantity"]
                    if item.get("taxable"):
                        price *= 1.2
                    if item.get("discount"):
                        price -= item["discount"]
                    total += price
                if total < 0:
                    total = 0.0
                return round(total, 2)
            """
        )
        workspace = tmp_path
        (workspace / "mod.py").write_text(
            body.replace("NAME", "first_total")
            + "\n\n"
            + body.replace("NAME", "second_total"),
            encoding="utf-8",
        )
        command = detector.build_command(
            binary, dc.replace(settings, roots=(".",), min_size=24)
        )
        result = subprocess.run(  # ruff: ignore[subprocess-without-shell-equals-true] - pinned repository-owned binary
            command,
            cwd=workspace,
            check=True,
            capture_output=True,
            text=True,
        )
        findings = detector.normalize_findings(json.loads(result.stdout))
        assert findings, "The planted copy must be reported."
        assert any(
            {location.name for location in finding.locations}
            == {"first_total", "second_total"}
            for finding in findings
        ), "The planted copy must name both duplicated functions."


class TestEndToEndBlocking:
    """The real detector driving the real `check` command.

    Every other blocking test substitutes a stub report or calls the detector
    without the gate, so none of them shows that a genuine duplicate reaches
    `check` and fails the build. These do, using the pinned binary.
    """

    DUPLICATE_BODY = textwrap.dedent(
        """\
        def NAME(items):
            total = 0.0
            for item in items:
                price = item["price"] * item["quantity"]
                if item.get("taxable"):
                    price *= 1.2
                if item.get("discount"):
                    price -= item["discount"]
                total += price
            if total < 0:
                total = 0.0
            return round(total, 2)
        """
    )

    def _planted_workspace(
        self, tmp_path: Path, *, member_count: int = 2, allow: str = ""
    ) -> Path:
        """Build a small gate workspace with the requested clone members."""
        workspace, _script = copied_gate_workspace(tmp_path)
        package = workspace / "planted"
        package.mkdir()
        (package / "__init__.py").write_text("", encoding="utf-8")
        for index in range(member_count):
            (package / f"total_{index}.py").write_text(
                self.DUPLICATE_BODY.replace("NAME", f"total_{index}"),
                encoding="utf-8",
            )
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
            )
            + allow,
            encoding="utf-8",
        )
        return workspace

    def _clean_workspace(self, tmp_path: Path) -> Path:
        """Build an unsaturated workspace with two unrelated small modules."""
        workspace, _script = copied_gate_workspace(tmp_path)
        package = workspace / "planted"
        package.mkdir()
        (package / "__init__.py").write_text("", encoding="utf-8")
        (package / "total.py").write_text(
            "def total(values):\n    return sum(values)\n", encoding="utf-8"
        )
        (package / "format_name.py").write_text(
            "def format_name(value):\n    return str(value).strip().title()\n",
            encoding="utf-8",
        )
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

    def _run_check(self, workspace: Path) -> subprocess.CompletedProcess[str]:
        """Run the copied gate's real `check` against the pinned detector."""
        settings = detector.load_settings(REPOSITORY_ROOT / "pyproject.toml")
        try:
            binary = detector.resolve_binary(settings)
        except detector.GateExecutionError as error:  # pragma: no cover
            pytest.skip(str(error))
        return run_gate_command(
            workspace / "scripts" / "duplication_gate.py",
            "check",
            environment=gate_environment(NOSE_BIN=binary),
        )

    def test_planted_duplicate_blocks_the_gate(self, tmp_path: Path) -> None:
        """A genuine duplicate fails `check` and names both copies."""
        result = self._run_check(self._planted_workspace(tmp_path))

        assert result.returncode == 1, (
            f"A planted duplicate must fail the gate.\n{result.stdout}{result.stderr}"
        )
        assert "planted/total_0.py" in result.stdout, (
            "The report must locate the duplicated file."
        )
        assert "make duplication-allow" in result.stdout, (
            "A blocking report must show how to record a reasoned exception."
        )

    def test_reasoned_exception_unblocks_the_planted_duplicate(
        self, tmp_path: Path
    ) -> None:
        """The same duplicate passes once a reasoned allow entry covers it."""
        allow = textwrap.dedent(
            """
            [[tool.duplication_gate.allow]]
            members = ["planted/total_0.py", "planted/total_1.py"]
            reason = "Planted fixture proving the gate blocks and allows."
            """
        )
        result = self._run_check(self._planted_workspace(tmp_path, allow=allow))

        assert result.returncode == 0, (
            f"A covered duplicate must pass.\n{result.stdout}{result.stderr}"
        )
        assert "duplication gate passed" in result.stdout, (
            "The gate must report its successful result."
        )

    def test_new_member_outside_the_reasoned_exception_blocks_again(
        self, tmp_path: Path
    ) -> None:
        """Adding a third file outside the narrow exception restores failure."""
        allow = textwrap.dedent(
            """
            [[tool.duplication_gate.allow]]
            members = ["planted/total_0.py", "planted/total_1.py"]
            reason = "The canary exception names only its original two members."
            """
        )
        result = self._run_check(
            self._planted_workspace(tmp_path, member_count=3, allow=allow)
        )
        assert result.returncode == 1, (
            f"An unlisted third member must block.\n{result.stdout}{result.stderr}"
        )
        assert "planted/total_2.py" in result.stdout, (
            "The blocking family must identify the new unlisted member."
        )

    def test_clean_workspace_passes(self, tmp_path: Path) -> None:
        """A clean, unsaturated workspace passes the same real gate."""
        result = self._run_check(self._clean_workspace(tmp_path))
        assert result.returncode == 0, f"{result.stdout}{result.stderr}"
        assert "duplication gate passed" in result.stdout, (
            "A clean workspace must report the successful gate result."
        )

    def test_repeated_scans_have_identical_normalized_output(
        self, tmp_path: Path
    ) -> None:
        """The same planted clone yields the same bounded gate report each time."""
        workspace = self._planted_workspace(tmp_path)
        results = [self._run_check(workspace) for _ in range(3)]
        normalized = [
            (result.returncode, result.stdout, result.stderr) for result in results
        ]
        assert normalized[1:] == normalized[:-1], (
            "Repeated scans must produce byte-for-byte stable wrapper output."
        )
