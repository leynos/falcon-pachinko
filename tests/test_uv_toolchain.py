"""Contract tests for the repository's uv-managed tooling.

The project moved from pip to uv-managed packaging so that dependency
resolution is reproducible and so that the estate's mutation-testing workflow,
which injects ``mutmut`` through ``uv run --with``, can resolve the project's
own dependency set from ``pyproject.toml`` and ``uv.lock``.

Three facts make the project uv-managed rather than pip-managed, and each is
silent when it breaks. A ``uv.lock`` that Git ignores is not distributed to
anyone. A ``make build`` that resolves past the lockfile leaves the committed
lockfile a suggestion, and CI installs the project by running ``make build``.
A Dependabot entry reading the wrong ecosystem stops producing lockfile
updates while still appearing configured.

``tests/test_toolchain_versions.py`` holds the neighbouring rule, that the
Makefile's tool pins and the CI pins name the same versions.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess  # ruff: ignore[suspicious-subprocess-import] - runs trusted repo tooling, never outside input
import tomllib
import typing as typ

import pytest
import yaml

from tests._makefile import (
    MAKEFILE,
    REPO_ROOT,
    CompletedRun,
    makefile_recipe,
    run_command,
    tool_environment,
)

if typ.TYPE_CHECKING:
    import pathlib

PYPROJECT = REPO_ROOT / "pyproject.toml"
UV_LOCK = REPO_ROOT / "uv.lock"
DEPENDABOT = REPO_ROOT / ".github" / "dependabot.yml"

#: The package ecosystems Dependabot offers for Python dependencies. It reads
#: `requirements*.txt` and the older `pyproject.toml` layout under `pip`, and
#: `uv.lock` under `uv`. Only one of them can be the entry for a directory.
PYTHON_ECOSYSTEMS = ("pip", "uv")


def _git(*arguments: str) -> CompletedRun:
    """Run a read-only ``git`` query in the repository root.

    Parameters
    ----------
    *arguments : str
        The arguments after ``git``.

    Returns
    -------
    CompletedRun
        The finished process with its captured standard output and error.
    """
    return run_command(["git", *arguments], env=tool_environment())


def _rule_declaration(target: str) -> str:
    """Return a Makefile rule's declaration line, with its prerequisites.

    :func:`~tests._makefile.makefile_recipe` returns the recipe, so it cannot
    see what a target depends on. The lockfile is asserted as a prerequisite
    rather than as a recipe line, so the build graph is read here.

    Parameters
    ----------
    target : str
        The Makefile target name.

    Returns
    -------
    str
        The declaration line, such as ``build: uv .venv uv.lock ## comment``.
    """
    text = MAKEFILE.read_text(encoding="utf-8")
    match = re.search(rf"^{re.escape(target)}:[^\n]*", text, flags=re.MULTILINE)
    assert match is not None, f"Makefile has no rule for {target}"
    return match.group(0)


def test_uv_lock_is_committed_and_not_ignored() -> None:
    """The lockfile must be a tracked file, not a local convenience.

    A lockfile that exists but is ignored is worse than none: it pins the
    author's environment while every other checkout keeps resolving freely,
    and nothing in the build says so.
    """
    assert UV_LOCK.is_file(), "uv.lock must exist; run `uv lock` at the root"

    # `--no-index` is load-bearing. Without it `git check-ignore` consults the
    # index and reports nothing for a path that is already tracked, so the
    # assertion would hold even with `uv.lock` back in `.gitignore` — which is
    # the regression it exists to catch.
    ignored = _git("check-ignore", "--no-index", "--quiet", "uv.lock")
    assert ignored.returncode == 1, (
        "uv.lock must not be ignored; `git check-ignore --no-index uv.lock` "
        "matched it, so the lockfile is pinned locally and distributed to "
        "nobody"
    )

    tracked = _git("ls-files", "--error-unmatch", "uv.lock")
    assert tracked.returncode == 0, (
        "uv.lock must be committed; `git ls-files --error-unmatch uv.lock` "
        f"failed: {tracked.stderr.strip()}"
    )


def test_uv_lock_resolves_the_declared_python_range() -> None:
    """The lockfile must cover the same interpreter range as the project.

    ``uv lock`` records the project's ``requires-python`` in the lockfile. A
    lockfile resolved for a narrower range excludes interpreters the package
    classifiers claim to support, and ``uv sync`` then refuses them at run
    time rather than at review time.
    """
    declared = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["project"][
        "requires-python"
    ]
    lockfile = tomllib.loads(UV_LOCK.read_text(encoding="utf-8"))

    assert lockfile["requires-python"] == declared, (
        f"uv.lock resolves {lockfile['requires-python']!r} but pyproject.toml "
        f"declares {declared!r}; re-run `uv lock`"
    )


def test_make_build_installs_the_locked_dependency_set() -> None:
    """``make build`` must refuse to resolve past the committed lockfile.

    CI installs the project by running ``make build``, so a sync that is free
    to re-resolve makes the committed lockfile advisory: the commit that
    generated it and the commit that installs it can disagree, and only the
    build log says which release was actually used.
    """
    recipe = makefile_recipe("build")

    assert "uv sync --group dev --locked" in recipe, (
        "make build must run `uv sync --group dev --locked`, so a lockfile "
        "that has fallen behind pyproject.toml fails instead of silently "
        f"re-resolving:\n{recipe}"
    )

    prerequisites = _rule_declaration("build").split(":", 1)[1].split("##")[0]
    assert "uv.lock" in prerequisites.split(), (
        "uv.lock must be a prerequisite of build, so a lockfile change is "
        f"visible in the build graph; the rule reads:\n{_rule_declaration('build')}"
    )


def _python_updates() -> list[dict[str, object]]:
    """Return the Dependabot entries naming a Python package ecosystem.

    Returns
    -------
    list of dict of str to object
        Every entry whose ``package-ecosystem`` is ``pip`` or ``uv``, in
        declaration order.
    """
    document = yaml.safe_load(DEPENDABOT.read_text(encoding="utf-8"))
    return [
        entry
        for entry in document["updates"]
        if entry.get("package-ecosystem") in PYTHON_ECOSYSTEMS
    ]


def test_dependabot_reads_python_updates_through_uv() -> None:
    """Exactly one entry, and it is the one that follows the lockfile.

    Dependabot's ``pip`` ecosystem reads ``requirements*.txt`` and the older
    ``pyproject.toml`` layout, while ``uv`` is the ecosystem that reads
    ``uv.lock``. An entry left on ``pip`` after the migration stops producing
    lockfile updates without reporting anything, and the lockfile ages
    quietly.
    """
    python_entries = _python_updates()
    ecosystems = [entry.get("package-ecosystem") for entry in python_entries]

    assert ecosystems == ["uv"], (
        "the Python dependency entry must use the uv ecosystem, and there "
        f"must be exactly one of them; found {ecosystems}"
    )
    assert python_entries[0]["directory"] == "/", (
        "the uv entry must read the repository root, where pyproject.toml "
        "and uv.lock live"
    )


def test_dependabot_python_entry_keeps_its_rollout_settings() -> None:
    """Changing ecosystem must not quietly drop the rollout's settings.

    The estate's Dependabot rollout sets a grouping, a pull-request limit,
    labels and a cooldown per ecosystem. Changing the ecosystem is a one-line
    edit made next to all of them, which is exactly where one gets dropped.
    """
    (entry,) = _python_updates()

    assert entry["groups"] == {
        "minor-and-patch": {"patterns": ["*"], "update-types": ["minor", "patch"]}
    }, "minor and patch updates must stay grouped into one pull request"
    assert entry["open-pull-requests-limit"] == 5, (
        "the rollout caps Python dependency pull requests at five"
    )
    assert entry["schedule"] == {"interval": "daily"}, (
        "the Python entry must keep its daily schedule"
    )

    # YAML hands back `object`, so the labels are narrowed before they are
    # compared: a scalar would otherwise iterate as characters and match.
    labels = entry["labels"]
    assert isinstance(labels, list), (
        f"the Python entry must declare labels as a list; got {labels!r}"
    )
    assert {"dependencies", "python"} <= {str(label) for label in labels}, (
        "the Python entry must keep its dependency labels"
    )
    assert entry["cooldown"] == {"default-days": 7, "semver-major-days": 14}, (
        "the default and major-update cooldowns are part of the rollout"
    )


def test_pytest_does_not_collect_the_generated_mutants_tree(
    tmp_path: pathlib.Path,
) -> None:
    """The mutation tree must stay out of the repository's own collection.

    ``make mutation`` copies every test module into ``mutants/`` under its
    original module name — mutmut runs the suite inside that tree — so a
    leftover run leaves two files claiming one import name. Pytest's default
    ``norecursedirs`` does not name ``mutants``, so it collects both, and the
    second import raises the ``ImportPathMismatchError`` that aborts
    ``make test`` before a single test runs. The collision belongs to the
    leftover, not to the code under test, so the setting that prevents it is
    asserted here.

    Pytest runs in a temporary project carrying this repository's own pytest
    configuration, so the committed setting is exercised rather than a copy
    that could drift out of step with it.
    """
    for tree in ("tests", "mutants/tests"):
        (tmp_path / tree).mkdir(parents=True)
        (tmp_path / tree / "test_shared_name.py").write_text(
            "def test_placeholder() -> None:\n    assert True\n",
            encoding="utf-8",
        )
    (tmp_path / "pyproject.toml").write_text(_pytest_configuration(), encoding="utf-8")

    collected = _run_collection(tmp_path)

    assert collected.returncode == 0, (
        "pytest must not recurse into the generated `mutants/` tree: a "
        "leftover run must not break `make test`. Collection said:\n"
        f"{collected.stdout}{collected.stderr}"
    )


def _pytest_configuration() -> str:
    """Return this repository's pytest settings as a standalone TOML document.

    Only ``[tool.pytest.ini_options]`` is carried: the temporary project has
    no packaging, so a copied ``[project]`` table would ask for a build
    backend that is not installed.

    Returns
    -------
    str
        The ini options, re-rendered as a TOML table that pytest reads from
        the temporary project's ``pyproject.toml``.
    """
    options = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["tool"]["pytest"][
        "ini_options"
    ]
    lines = ["[tool.pytest.ini_options]"]
    lines.extend(f"{key} = {_toml_value(value)}" for key, value in options.items())
    return "\n".join(lines) + "\n"


def _toml_value(value: object) -> str:
    """Render one ini value the way ``pyproject.toml`` would spell it.

    Only the value kinds this repository's pytest options actually use are
    handled, so a settings change that introduces another kind fails loudly
    here rather than silently rendering something pytest misreads. Dispatch is
    on the exact type, matching ``_safe_scalar`` in ``falcon_pachinko``:
    ``tomllib`` hands back only builtins, and ``bool`` must be matched before
    anything that would treat it as an ``int``.

    Returns
    -------
    str
        The TOML spelling of *value*.

    Raises
    ------
    TypeError
        If *value* is not a kind this renderer knows.
    """
    match value:
        case _ if type(value) is bool:
            return "true" if value else "false"
        case _ if type(value) is str:
            return json.dumps(value)
        case _ if type(value) is list:
            return "[" + ", ".join(_toml_value(item) for item in value) + "]"
        case _:
            msg = f"unsupported pytest ini value: {value!r}"
            raise TypeError(msg)


def _run_collection(project: pathlib.Path) -> CompletedRun:
    """Run pytest's collector in ``project`` and capture its exit status."""
    environment = tool_environment()
    executable = shutil.which("pytest", path=environment.get("PATH"))
    if executable is None:
        pytest.fail("pytest must be on PATH to run the collector")
    return subprocess.run(  # ruff: ignore[subprocess-without-shell-equals-true] - argv is repo tooling, never outside input
        [executable, "--collect-only", "-q", "-p", "no:cacheprovider"],
        cwd=project,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.parametrize("field", ["packages", "examples", "testing"])
def test_pyproject_declares_every_dependency_set(field: str) -> None:
    """The lockfile covers the dependency sets only while they are declared.

    ``uv lock`` resolves what ``pyproject.toml`` declares, so a dependency set
    that is absent from the file is absent from the lockfile, and the tools
    that consume it re-resolve freely.
    """
    project = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))

    if field == "packages":
        assert project["project"]["dependencies"], (
            "the runtime dependencies the lockfile resolves must be declared"
        )
        return

    assert field in project["project"]["optional-dependencies"], (
        f"the {field!r} extra must stay declared, so uv.lock keeps covering it"
    )
