"""Contract tests for the Makefile's lint and typecheck toolchain.

The Makefile pins ruff and ty via ``RUFF_VERSION`` and ``TY_VERSION`` while
the CI workflow installs the same tools with ``uv tool install <tool>==<v>``.
A version mismatch causes version-skew lint or typecheck failures without any
code change, so these tests assert both sites agree without hard-coding a
specific version.

Pylint runs in two passes: classic checks with vanilla Pylint on PyPy 8.0.0's
Python 3.12 build, and df12-python-lints on CPython 3.14. These tests pin the
structure of that arrangement; tests/test_lint_toolchain_integration.py runs
it against the real interpreters.
"""

from __future__ import annotations

import json
import re
import tomllib
import typing as typ

import pytest

from tests._makefile import (
    MAKEFILE,
    REPO_ROOT,
    makefile_pin,
    makefile_recipe,
    makefile_variable_block,
)

if typ.TYPE_CHECKING:
    import pathlib

CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"
PYPY_MANIFEST = REPO_ROOT / "tools" / "pypy-downloads.json"
CLASSIC_RCFILE = REPO_ROOT / "pyproject.toml"
DF12_RCFILE = REPO_ROOT / "pylintrc-df12.toml"
MARKDOWNLINT_CONFIG = REPO_ROOT / ".markdownlint-cli2.jsonc"

# Diagnostics that report an unparsable or unanalysable module. Disabling any
# of them lets such a module pass with exit status 0.
FAILED_ANALYSIS_MESSAGES = (
    "syntax-error",
    "fatal",
    "astroid-error",
    "parse-error",
    "config-parse-error",
    "method-check-failed",
)


def _ci_pin(tool: str) -> str:
    """Extract the pinned version a CI ``uv tool install`` step requests."""
    text = CI_WORKFLOW.read_text(encoding="utf-8")
    match = re.search(rf"uv tool install {re.escape(tool)}==(\S+)", text)
    if match is None:
        pytest.fail(f"ci.yml does not pin {tool} via 'uv tool install {tool}=='")
    return match.group(1)


def _messages_control(rcfile: pathlib.Path) -> dict[str, list[str]]:
    """Return an rcfile's Pylint ``messages control`` table."""
    config = tomllib.loads(rcfile.read_text(encoding="utf-8"))
    return config["tool"]["pylint"]["messages control"]


@pytest.mark.parametrize(
    ("makefile_variable", "tool"),
    [("RUFF_VERSION", "ruff"), ("TY_VERSION", "ty")],
)
def test_makefile_and_ci_pin_same_version(makefile_variable: str, tool: str) -> None:
    """The Makefile pin and the CI pin must name the same release."""
    makefile_version = makefile_pin(makefile_variable)
    ci_version = _ci_pin(tool)
    assert makefile_version == ci_version, (
        f"{tool} version pins have drifted: Makefile {makefile_variable} is "
        f"{makefile_version} but ci.yml installs {tool}=={ci_version}; "
        "bump both sites together"
    )


@pytest.mark.parametrize("variable", ["PYLINT_TOOL", "DF12_TOOL", "AMBRLEAKS"])
def test_pylint_tools_pin_pylint_and_astroid(variable: str) -> None:
    """Every Pylint tool environment resolves the pinned Pylint and Astroid.

    CI installs no Pylint of its own, so the Makefile is the single
    declaration. Astroid is pinned too: uv tool run would otherwise pick any
    release Pylint's range admits, and it must receive the same constraint.
    """
    assert makefile_pin("PYLINT_VERSION"), "PYLINT_VERSION must name a release"
    assert makefile_pin("ASTROID_VERSION"), "ASTROID_VERSION must name a release"

    command = makefile_variable_block(variable)
    for pin in ("pylint==$(PYLINT_VERSION)", "astroid==$(ASTROID_VERSION)"):
        assert pin in command, f"{variable} must pin {pin}, but reads:\n{command}"


def test_lint_runs_both_pylint_passes() -> None:
    """``make lint`` runs Ruff and both Pylint passes, not a manual second step."""
    recipe = makefile_recipe("lint")
    for step in ("$(RUFF) check", "lint-pylint", "lint-df12"):
        assert step in recipe, f"make lint must run {step}:\n{recipe}"


@pytest.mark.parametrize(
    ("target", "rcfile", "runtime_check"),
    [
        ("lint-pylint", "pyproject.toml", "$(PYLINT_RUNTIME_CHECK)"),
        ("lint-df12", "pylintrc-df12.toml", "$(DF12_RUNTIME_CHECK)"),
    ],
    ids=["classic", "df12"],
)
def test_pylint_pass_checks_its_runtime_then_lints_single_worker(
    target: str, rcfile: str, runtime_check: str
) -> None:
    """Each pass verifies its interpreter before running one Pylint worker.

    An explicit ``--rcfile`` stops either pass from picking up ambient
    configuration, such as a user's ``~/.pylintrc``.
    """
    recipe = makefile_recipe(target)
    check = recipe.find(f"tools/check_lint_runtime.py {runtime_check}")
    lint = recipe.find(f"pylint --rcfile={rcfile} --jobs=1 $(PYLINT_TARGETS)")

    assert check != -1, f"{target} must run the runtime check:\n{recipe}"
    assert lint != -1, f"{target} must lint with {rcfile} and one worker:\n{recipe}"
    assert check < lint, f"{target} must verify its runtime before linting"


def test_classic_pass_stops_on_the_first_failure() -> None:
    """The classic recipe is one shell script, so it must abort on failure."""
    assert makefile_recipe("lint-pylint").lstrip().startswith("set -eu;"), (
        "a failed runtime check must stop the classic pass before Pylint runs"
    )


@pytest.mark.parametrize("target", ["lint", "lint-pylint", "lint-df12"])
def test_lint_recipes_propagate_failure(target: str) -> None:
    """No lint recipe line may swallow a failing exit status."""
    recipe = makefile_recipe(target)
    for construct in ("exit-zero", "|| true", "|| :"):
        assert construct not in recipe, f"{target} must not use {construct!r}"
    for line in recipe.splitlines():
        assert not line.lstrip("\t").startswith("-"), (
            f"{target} must not ignore errors with a leading '-': {line!r}"
        )


def test_df12_plugin_is_loaded_only_by_its_own_rcfile() -> None:
    """The PyPy pass cannot inherit the df12 plugin from shared configuration."""
    classic = tomllib.loads(CLASSIC_RCFILE.read_text(encoding="utf-8"))
    df12 = tomllib.loads(DF12_RCFILE.read_text(encoding="utf-8"))

    classic_main = classic["tool"]["pylint"]["main"]
    assert "df12_python_lints" not in classic_main.get("load-plugins", []), (
        "pyproject.toml must not load the df12 plugin"
    )
    assert df12["tool"]["pylint"]["main"]["load-plugins"] == ["df12_python_lints"], (
        "pylintrc-df12.toml must load exactly the df12 plugin"
    )
    assert "--load-plugins" not in makefile_variable_block("PYLINT_TOOL"), (
        "the classic tool command must not load plugins itself"
    )


@pytest.mark.parametrize(
    "rcfile", [CLASSIC_RCFILE, DF12_RCFILE], ids=["classic", "df12"]
)
def test_rcfiles_pin_the_python_312_baseline(rcfile: pathlib.Path) -> None:
    """Version-sensitive checks follow the project baseline, not the host."""
    config = tomllib.loads(rcfile.read_text(encoding="utf-8"))
    assert config["tool"]["pylint"]["main"]["py-version"] == "3.12", (
        f"{rcfile} must pin py-version to the project's 3.12 baseline"
    )


@pytest.mark.parametrize(
    "rcfile", [CLASSIC_RCFILE, DF12_RCFILE], ids=["classic", "df12"]
)
def test_failed_analysis_is_enabled_and_never_disabled(rcfile: pathlib.Path) -> None:
    """Both passes fail, rather than skip, a module they cannot analyse."""
    control = _messages_control(rcfile)
    for message in FAILED_ANALYSIS_MESSAGES:
        assert message in control["enable"], f"{rcfile} must enable {message}"
        assert message not in control["disable"], f"{rcfile} must not disable {message}"

    makefile = MAKEFILE.read_text(encoding="utf-8")
    for message in FAILED_ANALYSIS_MESSAGES:
        assert f"--disable={message}" not in makefile, (
            f"the Makefile must not disable {message} on the command line"
        )


def test_pragma_names_are_validated_by_the_df12_pass() -> None:
    """W0012 moves to the pass that knows every message name.

    The classic pass cannot register df12 names, so it disables
    unknown-option-value; the df12 pass registers core and df12 messages and
    must keep checking every inline pragma name for typos.
    """
    assert "unknown-option-value" in _messages_control(CLASSIC_RCFILE)["disable"], (
        "the classic pass must not flag df12 names it never registers"
    )
    assert "unknown-option-value" in _messages_control(DF12_RCFILE)["enable"], (
        "the df12 pass must validate every pragma name"
    )


def test_pypy_manifest_matches_the_pinned_release() -> None:
    """Every manifest entry is the pinned PyPy build from its official source."""
    release = makefile_pin("PYPY_RELEASE")
    python_version = makefile_pin("PYPY_PYTHON_VERSION")
    entries = json.loads(PYPY_MANIFEST.read_text(encoding="utf-8"))

    assert f"pypy-{python_version}-linux-x86_64-gnu" in entries, (
        "the manifest must cover the CI runner platform"
    )
    for key, entry in entries.items():
        version = f"{entry['major']}.{entry['minor']}.{entry['patch']}"
        assert entry["name"] == "pypy", f"{key} must describe PyPy"
        assert entry["build"] == release, f"{key} must be PyPy {release}"
        assert version == python_version, f"{key} must be Python {python_version}"
        assert entry["url"].startswith(
            f"https://downloads.python.org/pypy/pypy3.12-v{release}-"
        ), f"{key} must download the official PyPy {release} tarball"
        assert re.fullmatch(r"[0-9a-f]{64}", entry["sha256"]), (
            f"{key} must carry a SHA-256 for uv to verify"
        )


def test_pypy_provisioning_is_isolated() -> None:
    """PyPy installs outside uv's shared store and never onto PATH."""
    provision = makefile_variable_block("PYPY_PROVISION")
    assert "--managed-python --no-bin" in provision, (
        "provisioning must restrict discovery to managed installs and skip bin links"
    )
    assert "UV_PYTHON_INSTALL_DIR=$(PYPY_INSTALL_DIR)" in makefile_variable_block(
        "PYPY_UV"
    ), "PyPy must install into its own directory"


def test_persisted_pylint_state_is_separated_by_runtime() -> None:
    """The two passes never share PYLINTHOME, and each names its runtime."""
    classic = re.search(r"PYLINTHOME=(\S+)", makefile_variable_block("PYLINT_TOOL"))
    df12 = re.search(r"PYLINTHOME=(\S+)", makefile_variable_block("DF12_TOOL"))
    assert classic is not None, "the classic pass must set PYLINTHOME"
    assert df12 is not None, "the df12 pass must set PYLINTHOME"
    assert classic.group(1) != df12.group(1), "the passes must not share state"
    assert "pypy" in classic.group(1), "the classic state must name its runtime"
    assert "cpython" in df12.group(1), "the df12 state must name its runtime"


def test_markdown_lint_skips_the_provisioned_interpreter() -> None:
    """PyPy ships README files that markdownlint-cli2 must not lint.

    `make markdownlint` passes one glob and leaves the exclusions to the
    configuration file, so that file is where the interpreter must be excluded.
    """
    config = json.loads(
        re.sub(
            r"^\s*//.*$",
            "",
            MARKDOWNLINT_CONFIG.read_text(encoding="utf-8"),
            flags=re.MULTILINE,
        )
    )
    assert ".uv-python/**" in config.get("ignores", []), (
        f"{MARKDOWNLINT_CONFIG.name} must exclude .uv-python"
    )


def test_mermaid_validation_skips_the_provisioned_interpreter() -> None:
    """PyPy ships README files that `make nixie` must not validate."""
    assert "-not -path './.uv-python/*'" in makefile_recipe("nixie"), (
        "make nixie must exclude .uv-python"
    )


def test_obsolete_pylint_pypy_shim_is_absent() -> None:
    """No dependency, invocation, or plugin entry for pylint-pypy-shim remains.

    PyPy 8.0.0 fixes the runtime bug the shim worked around, so vanilla
    Pylint runs there directly. The runtime checks name the shim's module
    only to forbid it.
    """
    sources = {
        path.relative_to(REPO_ROOT): path.read_text(encoding="utf-8")
        for path in (MAKEFILE, CLASSIC_RCFILE, DF12_RCFILE, CI_WORKFLOW)
    }
    for path, text in sources.items():
        for marker in ("pylint-pypy-shim", "PYLINT_PYPY_SHIM", "pylint-pypy "):
            assert marker not in text, f"{path} must not reference {marker!r}"
    for rcfile in (CLASSIC_RCFILE, DF12_RCFILE):
        main = tomllib.loads(rcfile.read_text(encoding="utf-8"))["tool"]["pylint"]
        assert "pylint_pypy_shim" not in main["main"].get("load-plugins", []), (
            f"{rcfile.name} must not load the shim plugin"
        )


def _ci_step(name: str) -> str:
    """Return the text of a named CI step, up to the next step."""
    text = CI_WORKFLOW.read_text(encoding="utf-8")
    match = re.search(
        rf"^ {{6}}- name: {re.escape(name)}\n(?P<body>(?: {{8}}[^\n]*\n)+)",
        text,
        flags=re.MULTILINE,
    )
    if match is None:
        pytest.fail(f"ci.yml has no step named {name!r}")
    return match.group("body")


@pytest.mark.parametrize(
    ("step", "command"),
    [
        ("Run linters", "make lint"),
        ("Test lint toolchain", "uv run pytest -m lint_toolchain"),
    ],
)
def test_ci_requires_the_lint_toolchain(step: str, command: str) -> None:
    """CI runs both Pylint passes and their integration tests as hard gates."""
    body = _ci_step(step)
    assert command in body, f"the {step!r} step must run {command!r}:\n{body}"
    assert "continue-on-error" not in body, f"the {step!r} step must be able to fail"


@pytest.mark.parametrize(
    ("step", "identity_files"),
    [
        ("Cache PyPy lint interpreter", ("tools/pypy-downloads.json",)),
        (
            "Cache lint tool environments",
            ("Makefile", "pylintrc-df12.toml", "tools/pypy-downloads.json"),
        ),
    ],
)
def test_ci_lint_caches_key_on_toolchain_identity(
    step: str, identity_files: tuple[str, ...]
) -> None:
    """A toolchain pin change must never restore a stale lint cache."""
    body = _ci_step(step)
    key = re.search(r"key: (?P<key>[^\n]+)", body)
    assert key is not None, f"the {step!r} step must declare a cache key"
    for part in ("runner.os", "runner.arch", *identity_files):
        assert part in key["key"], f"the {step!r} key must include {part}"
    assert "restore-keys" not in body, (
        f"the {step!r} step must not fall back to a different toolchain's cache"
    )
