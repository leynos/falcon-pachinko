"""Read which Python each generate-coverage call measures on.

Support for ``test_coverage_python_version.py``: the resolver's source order,
the reading of every call's declared sources, the verdict on whether they
agree, and the version comparison. It knows nothing about this repository's
lanes; the tests apply it to them.
"""

from __future__ import annotations

import tomllib
import typing as typ
from pathlib import Path

import yaml
from packaging.specifiers import SpecifierSet
from packaging.version import Version

from .strict_yaml import StrictLoader

ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = ROOT / ".github" / "workflows"
#: The pull-request lane and the publisher whose baseline it ratchets against.
LANES: typ.Final[tuple[str, ...]] = ("ci.yml", "coverage-main.yml")
SETUP_PYTHON: typ.Final[str] = "actions/setup-python@"
GENERATE_COVERAGE: typ.Final[str] = "/.github/actions/generate-coverage@"
#: The resolver's order, highest priority first.
SOURCES: typ.Final[tuple[str, ...]] = (
    "input",
    "UV_PYTHON",
    ".python-version",
    "setup-python",
)


class CoverageCall(typ.NamedTuple):
    """One generate-coverage call and the versions each source declares for it.

    Parameters
    ----------
    job : str
        The name of the job that contains the call.
    sources : dict[str, str]
        The version each resolver source declares, in priority order, with an
        empty string where a source declares nothing.

    """

    job: str
    sources: dict[str, str]

    @property
    def declared(self) -> dict[str, str]:
        """The sources that name a version."""
        return {name: version for name, version in self.sources.items() if version}

    @property
    def effective(self) -> str:
        """The version the resolver would choose, or empty."""
        return next(iter(self.declared.values()), "")


def verdict(call: CoverageCall) -> str:
    """Return why a call's declared sources fail the contract, or empty.

    ``"undeclared"`` means no source names a version, so the call would
    measure on whatever Python the runner happens to have; ``"conflicting"``
    means two sources name different versions, so a higher-priority value
    silently overrides a lower one.

    Parameters
    ----------
    call : CoverageCall
        The call to judge.

    Returns
    -------
    str
        ``"undeclared"``, ``"conflicting"``, or ``""`` when every declared
        source agrees.

    """
    declared = set(call.declared.values())
    if not declared:
        return "undeclared"
    return "conflicting" if len(declared) > 1 else ""


def requires_python(pyproject: str) -> SpecifierSet:
    """Return the project's ``requires-python`` specifier set.

    Parameters
    ----------
    pyproject : str
        The text of ``pyproject.toml``.

    Returns
    -------
    SpecifierSet
        The versions the project declares it accepts.

    """
    return SpecifierSet(tomllib.loads(pyproject)["project"]["requires-python"])


def read_required_text(path: Path) -> str:
    """Return the text of a file the contract cannot run without.

    With :func:`read_text_if_present`, this is the module's file boundary. A
    missing or undecodable file raises (``OSError`` or ``UnicodeDecodeError``
    from the read), so the contract fails loudly instead of judging nothing.

    Parameters
    ----------
    path : Path
        The file to read.

    Returns
    -------
    str
        The file's UTF-8 text.

    """
    return path.read_text(encoding="utf-8")


def read_text_if_present(path: Path) -> str | None:
    """Return a file's text, or ``None`` when the file does not exist.

    Only a missing file reads as absent. A directory, a permission failure or
    undecodable bytes raise (``OSError`` or ``UnicodeDecodeError``), which fails
    the contract loudly rather than reading as absent.

    Parameters
    ----------
    path : Path
        The file to read.

    Returns
    -------
    str or None
        The file's text, or ``None`` when it is absent.

    """
    try:
        return read_required_text(path)
    except FileNotFoundError:
        return None


def python_version_entry(text: str | None) -> str:
    """Return the first non-comment entry of ``.python-version`` text, or empty.

    Pure: the caller reads the file (see :func:`read_text_if_present`), so the
    parsing is tested on text alone. ``None`` means the file is absent.

    Parameters
    ----------
    text : str or None
        The file's text, or ``None`` when the file is absent.

    Returns
    -------
    str
        The first entry that is not blank or a comment, or ``""``.

    """
    entries = (line.strip() for line in (text or "").splitlines())
    return next((entry for entry in entries if entry and not entry.startswith("#")), "")


def _mapping(value: object) -> dict[str, object]:
    """Return ``value`` when it is a mapping, otherwise an empty one."""
    return typ.cast("dict[str, object]", value) if isinstance(value, dict) else {}


def _declared_by_setup(step: dict[str, object]) -> str:
    """Return the version a setup-python step reliably puts on ``PATH``, or empty."""
    if "if" in step or step.get("continue-on-error"):
        return ""
    return str(_mapping(step.get("with")).get("python-version") or "")


def _uv_python(*scopes: dict[str, object]) -> str:
    """Return the innermost ``UV_PYTHON`` among step, job and workflow scopes.

    The first scope that defines the key wins even when its value is empty: an
    empty step value replaces the outer one, and the action then falls through
    to ``.python-version`` or ``PATH`` rather than to the outer value.

    Returns
    -------
    str
        The innermost defined value, or ``""`` when no scope defines the key.

    """
    for scope in scopes:
        env = _mapping(scope.get("env"))
        if "UV_PYTHON" in env:
            return str(env["UV_PYTHON"] or "")
    return ""


def _job_calls(
    name: str, job: dict[str, object], document: dict[str, object], python_version: str
) -> list[CoverageCall]:
    """Return one job's coverage calls, reading its steps in order."""
    calls: list[CoverageCall] = []
    on_path = ""
    for step in _mapping_list(job.get("steps")):
        uses = str(step.get("uses", ""))
        if uses.startswith(SETUP_PYTHON):
            on_path = _declared_by_setup(step)
        elif GENERATE_COVERAGE in uses:
            versions = (
                str(_mapping(step.get("with")).get("python-version") or ""),
                _uv_python(step, job, document),
                python_version,
                on_path,
            )
            calls.append(CoverageCall(name, dict(zip(SOURCES, versions, strict=True))))
    return calls


def _mapping_list(value: object) -> list[dict[str, object]]:
    """Return the mappings in ``value`` when it is a list, otherwise none."""
    items = value if isinstance(value, list) else []
    return [
        typ.cast("dict[str, object]", item) for item in items if isinstance(item, dict)
    ]


def coverage_calls(workflow: str, python_version: str = "") -> list[CoverageCall]:
    """Return every generate-coverage call in a workflow with its declared sources.

    Each job's steps are read in order. A setup-python step that always runs
    replaces the Python on ``PATH`` for the steps after it; one that may be
    skipped or fail green clears it, since the call cannot rely on it.

    Parameters
    ----------
    workflow : str
        The text of one workflow file.
    python_version : str
        The repository's ``.python-version`` entry, or empty.

    Returns
    -------
    list of CoverageCall
        One entry per call, in workflow order, with every source's version in
        the resolver's priority order (empty where a source declares nothing).

    """
    document = yaml.load(workflow, Loader=StrictLoader) or {}  # ruff: ignore[unsafe-yaml-load] - strict SafeLoader subclass
    return [
        call
        for name, job in (document.get("jobs") or {}).items()
        for call in _job_calls(name, job, document, python_version)
    ]


def rejected_versions(accepted: SpecifierSet, requested: list[str]) -> list[str]:
    """Return the requested versions the specifier set does not accept.

    Parameters
    ----------
    accepted : SpecifierSet
        The project's ``requires-python``.
    requested : list of str
        Versions to check.

    Returns
    -------
    list of str
        The requested versions outside ``accepted``, in their original order.

    """
    return [version for version in requested if Version(version) not in accepted]
