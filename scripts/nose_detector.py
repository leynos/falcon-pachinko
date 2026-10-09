"""Pinned ``nose`` duplication detector used by the code-duplication gate.

This module owns everything that touches the external detector: reading the
repository's ``[tool.nose]`` settings, locating the pinned binary, verifying
its version, and running one ``nose query``. The report schema lives in
``scripts/nose_schema.py``.

Adapted from ``leynos/episodic`` PR #276 at commit
``d9e5ac0d254f375e2986f52d91a3b88c117c833b``.
"""

from __future__ import annotations

import dataclasses as dc
import json
import os
import shutil
import subprocess  # ruff: ignore[suspicious-subprocess-import] - gate runs the pinned repository-owned binary
import tomllib
from collections import abc as cabc
from pathlib import Path

from nose_schema import (
    Finding,
    GateConfigError,
    GateExecutionError,
    Location,
    is_non_empty_string,
    is_positive_integer,
    is_safe_relative_posix_path,
    normalize_findings,
    require_string_tuple,
    require_table,
    require_value,
)
from pathspec import GitIgnoreSpec

REPO_ROOT = Path(__file__).resolve().parents[1]
PYPROJECT = REPO_ROOT / "pyproject.toml"
INSTALL_HINT = "run `make install-nose` to install the pinned detector"
COMMAND_TIMEOUT_SECONDS = 120
_SUPPORTED_CHANNELS = {"syntax", "semantic", "near"}

type CommandRunner = cabc.Callable[
    [cabc.Sequence[str], Path, cabc.Mapping[str, str]], str
]
type BinaryDiscoverer = cabc.Callable[[Path, cabc.Mapping[str, str]], str | None]


@dc.dataclass(frozen=True, slots=True)
class DetectorContext:
    """Explicit runtime dependencies for binary discovery and query execution."""

    repository_root: Path
    environment: cabc.Mapping[str, str]
    binary_discoverer: BinaryDiscoverer
    command_runner: CommandRunner


__all__ = [
    "BinaryDiscoverer",
    "CommandRunner",
    "DetectorContext",
    "Finding",
    "GateConfigError",
    "GateExecutionError",
    "Location",
    "NoseSettings",
    "build_command",
    "load_settings",
    "normalize_findings",
    "resolve_binary",
    "run_detector",
]


@dc.dataclass(frozen=True, slots=True)
class NoseSettings:
    """Gate settings read from ``[tool.nose]``.

    Attributes
    ----------
    version : str
        Version string the installed binary must report.
    roots : tuple[str, ...]
        Repository-relative paths handed to ``nose query``.
    mode : str
        Comma-separated detection channels, pinned so a change to nose's
        defaults cannot silently widen or narrow the gate.
    min_size : int
        Smallest unit size, in nose IL tokens, that may be reported.
    surface : str
        ``"default"`` for nose's ranked dashboard, or ``"all"`` to include
        hidden-surface families.
    top : int | None
        How many ranked families to adjudicate, or ``None`` for nose's own
        view size.
    exclude : tuple[str, ...]
        Gitignore-style globs excluded from the scan.
    """

    version: str
    roots: tuple[str, ...]
    mode: str
    min_size: int
    surface: str
    top: int | None
    exclude: tuple[str, ...]


def load_settings(pyproject_path: Path) -> NoseSettings:
    """Load the detector settings from ``[tool.nose]``.

    Parameters
    ----------
    pyproject_path : pathlib.Path
        Path to the repository ``pyproject.toml``.

    Returns
    -------
    NoseSettings
        Validated detector settings.

    """
    return _settings_from_table(
        _load_nose_table(pyproject_path), repository_root=pyproject_path.parent
    )


def _load_nose_table(pyproject_path: Path) -> cabc.Mapping[str, object]:
    """Read TOML and validate the nested table shape before field checks.

    Returns
    -------
    collections.abc.Mapping[str, object]
        The validated ``[tool.nose]`` table.
    """
    with pyproject_path.open("rb") as handle:
        data = tomllib.load(handle)
    root = require_table(data, context="pyproject")
    table = require_table(root.get("tool", {}), context="tool")
    return require_table(table.get("nose", {}), context="tool.nose")


def _settings_from_table(
    nose: cabc.Mapping[str, object], *, repository_root: Path
) -> NoseSettings:
    """Validate detector fields and source roots in their reported order.

    Returns
    -------
    NoseSettings
        Validated detector settings.

    Raises
    ------
    GateConfigError
        If a setting is invalid or a configured root selects no source files.
    """
    surface = require_value(
        nose.get("surface", "all"),
        validator=is_non_empty_string,
        expected="a non-empty string",
        context="tool.nose.surface",
    )
    if surface not in {"default", "all"}:
        msg = "tool.nose.surface must be 'default' or 'all'"
        raise GateConfigError(msg)
    roots = require_string_tuple(nose.get("roots"), context="tool.nose.roots")
    if not roots:
        msg = "tool.nose.roots must not be empty"
        raise GateConfigError(msg)
    mode = require_value(
        nose.get("mode"),
        validator=is_non_empty_string,
        expected="a non-empty string",
        context="tool.nose.mode",
    )
    _validate_channels(mode)
    exclude = require_string_tuple(nose.get("exclude", []), context="tool.nose.exclude")
    _validate_roots(roots, exclude, repository_root=repository_root)
    return NoseSettings(
        version=require_value(
            nose.get("version"),
            validator=is_non_empty_string,
            expected="a non-empty string",
            context="tool.nose.version",
        ),
        roots=roots,
        mode=mode,
        min_size=require_value(
            nose.get("min-size"),
            validator=is_positive_integer,
            expected="a positive integer",
            context="tool.nose.min-size",
        ),
        surface=surface,
        top=(
            None
            if nose.get("top") is None
            else require_value(
                nose.get("top"),
                validator=is_positive_integer,
                expected="a positive integer",
                context="tool.nose.top",
            )
        ),
        exclude=exclude,
    )


def _validate_roots(
    roots: tuple[str, ...],
    exclude: tuple[str, ...],
    *,
    repository_root: Path,
) -> None:
    """Reject mistyped roots and roots that select no Python source files."""
    root_directory = repository_root.resolve()
    exclude_spec = _validate_excludes(exclude)

    for root in roots:
        candidate, resolved = _resolve_root(
            root, repository_root=repository_root, root_directory=root_directory
        )
        _validate_root_sources(root, candidate, resolved, exclude_spec)


def _resolve_root(
    root: str, *, repository_root: Path, root_directory: Path
) -> tuple[Path, Path]:
    """Resolve one safe configured root and reject missing or escaping paths."""
    if not is_safe_relative_posix_path(root):
        msg = f"tool.nose.roots entry {root!r} must be a repository-relative POSIX path"
        raise GateConfigError(msg)
    candidate = repository_root / root
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as error:
        msg = f"tool.nose.roots entry {root!r} does not exist: {error}"
        raise GateConfigError(msg) from error
    if not resolved.is_relative_to(root_directory):
        msg = f"tool.nose.roots entry {root!r} escapes the repository"
        raise GateConfigError(msg)
    return candidate, resolved


def _validate_root_sources(
    root: str,
    candidate: Path,
    resolved: Path,
    exclude_spec: GitIgnoreSpec | None,
) -> None:
    """Require one in-scope Python file beneath a configured root."""
    source_files, is_file_root = _root_sources(candidate, resolved)
    ignore_cache: dict[Path, GitIgnoreSpec | None] = {}
    for source in source_files:
        if not _is_selected_source(
            source,
            resolved_root=resolved,
            is_file_root=is_file_root,
            exclude_spec=exclude_spec,
        ):
            continue
        if is_file_root or not _is_gitignored(source, candidate, ignore_cache):
            return
    msg = f"tool.nose.roots entry {root!r} selects no Python source files"
    raise GateConfigError(msg)


def _validate_excludes(exclude: tuple[str, ...]) -> GitIgnoreSpec | None:
    """Validate POSIX exclusion paths and compile their Git-ignore matcher."""
    for pattern in exclude:
        if not is_safe_relative_posix_path(pattern):
            msg = "tool.nose.exclude entries must be non-escaping POSIX globs"
            raise GateConfigError(msg)
    if not exclude:
        return None
    try:
        return GitIgnoreSpec.from_lines(exclude)
    except ValueError as error:
        msg = f"tool.nose.exclude entries must be valid Git-ignore globs: {error}"
        raise GateConfigError(msg) from error


def _root_sources(candidate: Path, resolved: Path) -> tuple[list[Path], bool]:
    """List Python candidates for a configured file or directory root."""
    if resolved.is_file():
        return [candidate], True
    if resolved.is_dir():
        return list(candidate.rglob("*.py")), False
    return [], False


def _is_selected_source(
    source: Path,
    *,
    resolved_root: Path,
    is_file_root: bool,
    exclude_spec: GitIgnoreSpec | None,
) -> bool:
    """Report whether one source is in scope after safety and glob checks."""
    if not source.is_file() or source.suffix != ".py":
        return False
    resolved_source = source.resolve()
    if is_file_root and resolved_source != resolved_root:
        return False
    if not is_file_root and not resolved_source.is_relative_to(resolved_root):
        return False
    relative_path = (
        source.name
        if is_file_root
        else resolved_source.relative_to(resolved_root).as_posix()
    )
    return not _is_excluded(relative_path, exclude_spec)


def _is_gitignored(
    source: Path,
    scan_root: Path,
    ignore_cache: dict[Path, GitIgnoreSpec | None],
) -> bool:
    """Report whether nose's root-relative ignore files exclude ``source``.

    Only ignore files at or below the configured root apply. A directory
    excluded by its parent cannot be reopened by a nested ``.gitignore``,
    matching Git's traversal rules and nose's effective source selection.

    Returns
    -------
    bool
        Whether the source is ignored by the applicable in-root rules.
    """
    relative_parts = source.relative_to(scan_root).parts
    current_directory = scan_root
    scoped_specs: list[tuple[Path, GitIgnoreSpec]] = []
    _append_gitignore_spec(scan_root, scoped_specs, ignore_cache)

    for index, part in enumerate(relative_parts):
        path = current_directory / part
        is_directory = index < len(relative_parts) - 1
        if _matches_ignore_rules(
            path, is_directory=is_directory, scoped_specs=scoped_specs
        ):
            return True
        if is_directory:
            current_directory = path
            _append_gitignore_spec(current_directory, scoped_specs, ignore_cache)
    return False


def _append_gitignore_spec(
    directory: Path,
    scoped_specs: list[tuple[Path, GitIgnoreSpec]],
    ignore_cache: dict[Path, GitIgnoreSpec | None],
) -> None:
    """Add one directory's ignore rules to the root-to-leaf rule stack."""
    spec = _load_gitignore_spec(directory, ignore_cache)
    if spec is not None:
        scoped_specs.append((directory, spec))


def _matches_ignore_rules(
    path: Path,
    *,
    is_directory: bool,
    scoped_specs: list[tuple[Path, GitIgnoreSpec]],
) -> bool:
    """Return the latest matching decision from the in-scope ignore files.

    Returns
    -------
    bool
        Whether the path is ignored after applying rules from root to leaf.
    """
    ignored = False
    for base, spec in scoped_specs:
        scoped_path = path.relative_to(base).as_posix()
        if is_directory:
            scoped_path += "/"
        match = spec.check_file(scoped_path).include
        if match is not None:
            ignored = match
    return ignored


def _load_gitignore_spec(
    directory: Path,
    cache: dict[Path, GitIgnoreSpec | None],
) -> GitIgnoreSpec | None:
    """Read and cache one in-root Git ignore file for source validation."""
    if directory in cache:
        return cache[directory]
    ignore_file = directory / ".gitignore"
    if not ignore_file.exists():
        cache[directory] = None
        return None
    try:
        patterns = ignore_file.read_text(encoding="utf-8").splitlines()
        spec = GitIgnoreSpec.from_lines(patterns)
    except (OSError, UnicodeError, ValueError) as error:
        msg = f"cannot read {ignore_file} while validating nose scan roots: {error}"
        raise GateConfigError(msg) from error
    cache[directory] = spec
    return spec


def _is_excluded(path: str, spec: GitIgnoreSpec | None) -> bool:
    """Report whether a scan-root-relative source path matches an exclusion."""
    return spec is not None and spec.match_file(path)


def _validate_channels(mode: str) -> None:
    """Reject empty, repeated, or unsupported detector channels."""
    if not _has_supported_channels(mode):
        msg = "tool.nose.mode must list unique syntax, semantic, or near channels"
        raise GateConfigError(msg)


def _has_supported_channels(mode: str) -> bool:
    """Check channel content and uniqueness independently of diagnostics."""
    channels = tuple(channel.strip() for channel in mode.split(","))
    return (
        bool(channels)
        and all(channels)
        and len(set(channels)) == len(channels)
        and set(channels) <= _SUPPORTED_CHANNELS
    )


def resolve_binary(
    settings: NoseSettings, *, context: DetectorContext | None = None
) -> str:
    """Locate the pinned nose binary and verify its version.

    Parameters
    ----------
    settings : NoseSettings
        Detector settings supplying the pinned version.
    context : DetectorContext | None
        Explicit repository root, environment, binary discovery and command
        runner. The default context is assembled at the CLI boundary.

    Returns
    -------
    str
        Path to a nose binary reporting the pinned version.

    Raises
    ------
    GateExecutionError
        If no binary is found or the reported version does not match.
    """
    runtime = _default_context() if context is None else context
    # Resolve against the repository root so a relative NOSE_BIN keeps
    # working for callers that run the detector from another directory.
    override = runtime.environment.get("NOSE_BIN")
    if override:
        override_path = Path(override)
        if not override_path.is_absolute():
            override_path = runtime.repository_root / override_path
        candidate = str(override_path.resolve())
    else:
        candidate = runtime.binary_discoverer(
            runtime.repository_root, runtime.environment
        )
    if candidate is None:
        default_binary = runtime.repository_root / ".tools" / "nose" / "nose"
        msg = (
            f"nose {settings.version} was not found at {default_binary} "
            f"or on PATH: {INSTALL_HINT}"
        )
        raise GateExecutionError(msg)
    reported = runtime.command_runner(
        [candidate, "--version"], runtime.repository_root, runtime.environment
    ).strip()
    expected = f"nose {settings.version}"
    if reported != expected:
        msg = (
            f"{candidate} reports '{reported}' but the gate pins "
            f"'{expected}': {INSTALL_HINT}"
        )
        raise GateExecutionError(msg)
    return candidate


def _default_context() -> DetectorContext:
    """Assemble process-wide dependencies at the detector's CLI boundary."""
    return DetectorContext(
        repository_root=REPO_ROOT,
        environment=dict(os.environ),
        binary_discoverer=_discover_binary,
        command_runner=_run_command,
    )


def _discover_binary(
    repository_root: Path, environment: cabc.Mapping[str, str]
) -> str | None:
    """Return the repository-local nose binary, else one found on supplied PATH."""
    local_binary = repository_root / ".tools" / "nose" / "nose"
    if local_binary.is_file():
        return str(local_binary)
    return shutil.which("nose", path=environment.get("PATH", os.defpath))


def build_command(binary: str, settings: NoseSettings) -> list[str]:
    """Build the ``nose query`` command for the configured gate settings.

    Parameters
    ----------
    binary : str
        Path to the verified nose binary.
    settings : NoseSettings
        Detector settings for this repository.

    Returns
    -------
    list[str]
        Argument vector for the detector run.
    """
    command = [binary, "query"]
    for root in settings.roots:
        command.extend(("--root", root))
    if settings.surface == "all":
        # The bare `all` term unhides families nose keeps off its dashboard.
        command.append("all")
    if settings.top is not None:
        command.append(f"top={settings.top}")
    command.extend(("--mode", settings.mode))
    command.extend(("--min-size", str(settings.min_size)))
    for glob in settings.exclude:
        command.extend(("--exclude", glob))
    command.extend(("--format", "json"))
    return command


def run_detector(
    settings: NoseSettings,
    *,
    context: DetectorContext | None = None,
) -> list[Finding]:
    """Run the pinned detector and normalize its report.

    Parameters
    ----------
    settings : NoseSettings
        Detector settings for this repository.
    context : DetectorContext | None
        Explicit repository root, environment, binary discovery and command
        runner. The default context is assembled at the CLI boundary.

    Returns
    -------
    list[Finding]
        Findings ordered by descending value, then by source location.

    Raises
    ------
    GateExecutionError
        If the detector cannot be run or emits unreadable output. Report
        schema violations propagate as ``GateConfigError`` from
        :func:`normalize_findings`.
    """
    runtime = _default_context() if context is None else context
    binary = resolve_binary(settings, context=runtime)
    output = runtime.command_runner(
        build_command(binary, settings), runtime.repository_root, runtime.environment
    )
    try:
        report = json.loads(output)
    except json.JSONDecodeError as error:
        msg = f"nose report is not valid JSON: {error}"
        raise GateExecutionError(msg) from error
    return normalize_findings(report)


def _run_command(
    command: cabc.Sequence[str],
    repository_root: Path,
    environment: cabc.Mapping[str, str],
) -> str:
    """Run one detector command in its supplied repository and environment."""
    try:
        result = subprocess.run(  # ruff: ignore[subprocess-without-shell-equals-true] - executes the fixed repository-owned binary
            list(command),
            cwd=repository_root,
            env=dict(environment),
            check=False,
            capture_output=True,
            text=True,
            timeout=COMMAND_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as error:
        msg = f"{command[0]} timed out after {COMMAND_TIMEOUT_SECONDS} seconds"
        raise GateExecutionError(msg) from error
    except OSError as error:
        msg = f"cannot run {command[0]}: {error}: {INSTALL_HINT}"
        raise GateExecutionError(msg) from error
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        msg = f"{command[0]} exited with status {result.returncode}: {detail}"
        raise GateExecutionError(msg)
    return result.stdout
