"""Integration tests for the two-pass Pylint toolchain.

`make lint` runs the classic Pylint checks with vanilla Pylint on PyPy 8.0.0's
Python 3.12 build, and the df12-python-lints checks on CPython 3.14. These
tests drive the Makefile's own commands against the real interpreters. They
prove that each pass runs where it claims to, analyses Python 3.12 syntax
rather than skipping it, fails on broken input, keeps the df12 plugin out of
the PyPy pass, and leaves the project virtual environment alone.

The first run provisions PyPy through the Makefile, so it needs network access
once. The tests never skip: a missing interpreter is a failure, not a pass.
"""

from __future__ import annotations

import json
import pathlib
import re
import textwrap

import pytest

from tests._makefile import (
    REPO_ROOT,
    CompletedRun,
    make_expand,
    makefile_pin,
    run_command,
    run_make,
    split_command,
    tool_environment,
)

pytestmark = [pytest.mark.lint_toolchain]

# Emit one finding per line as `path:msg-id:symbol`, so assertions do not
# depend on Pylint's human-readable layout.
_MSG_TEMPLATE = "--msg-template={path}:{msg_id}:{symbol}"

_FIXTURES = {
    # Python 3.12 syntax alongside a deliberate classic-check violation.
    "pep695_violation.py": '''
        """Python 3.12 syntax beside a no-else-return violation."""

        type Pair[T] = tuple[T, T]


        def first[T](pair: Pair[T]) -> T:
            """Return the first element of a pair."""
            return pair[0]


        class Box[T]:
            """Hold a single value."""

            def __init__(self, value: T) -> None:
                self.value = value


        def sign(value: int) -> int:
            """Classify an integer; the else after return is the violation."""
            if value < 0:
                return -1
            else:
                return 1
    ''',
    # A bare assert, which only the df12 pass reports.
    "bare_assert.py": '''
        """A bare assert that the df12 plugin reports."""


        def check(value: int) -> None:
            """Assert without a failure message."""
            assert value > 0
    ''',
    # Baseline-3.12 code that genuinely needs deferred annotations.
    "future_annotations.py": '''
        """A baseline-3.12 module that relies on deferred annotations."""

        from __future__ import annotations


        class Node:
            """Refer to its own type in an annotation."""

            def link(self, other: Node) -> Node:
                """Return the other node."""
                return other
    ''',
    # Deliberately invalid syntax, kept out of normal lint discovery.
    "broken.py": "def broken(:\n    pass\n",
}

_ASTROID_PROBE = """
import json
import pathlib
import sys
import types

import astroid
from astroid import MANAGER, nodes

def params(node):
    return [param.name.name for param in node.type_params]


def inferred(expression):
    return sorted({i.qname() for i in astroid.extract_node(expression).infer()})


source = pathlib.Path(sys.argv[1]).read_text(encoding="utf-8")
module = astroid.parse(source)
alias = next(module.nodes_of_class(nodes.TypeAlias))
defs = {n.name: n for n in module.nodes_of_class((nodes.FunctionDef, nodes.ClassDef))}
builtins = MANAGER.ast_from_module_name("builtins")
list_class = builtins["list"]
report = {
    "implementation": sys.implementation.name,
    "alias": [alias.name.name, params(alias)],
    "function": ["first", params(defs["first"])],
    "class": ["Box", params(defs["Box"])],
    "text_signature": types.FunctionType.__text_signature__,
    "builtins_locals": len(builtins.locals),
    "list": [type(list_class).__name__, "__class_getitem__" in list_class.locals],
    "list_subscript": inferred("list[int]"),
    "dict_subscript": inferred("dict[str, int]"),
}
print(json.dumps(report))
"""


def _findings(output: str) -> set[tuple[str, str]]:
    """Return ``(file name, message id)`` pairs from templated Pylint output."""
    pattern = re.compile(r"^(?P<path>[^\s:]+):(?P<id>[A-Z]\d{4}):", re.MULTILINE)
    return {
        (pathlib.Path(match["path"]).name, match["id"])
        for match in pattern.finditer(output)
    }


def _run_tool(env: dict[str, str], argv: list[str], *arguments: str) -> CompletedRun:
    """Run a Makefile tool command with extra arguments from the repo root."""
    return run_command([*argv, *arguments], env=env)


def _venv_identity() -> dict[str, str]:
    """Describe the project virtual environment's interpreter."""
    venv = REPO_ROOT / ".venv"
    return {
        "pyvenv.cfg": (venv / "pyvenv.cfg").read_text(encoding="utf-8"),
        "python": str((venv / "bin" / "python").resolve()),
    }


@pytest.fixture(scope="module")
def venv_before() -> dict[str, str]:
    """Record the project virtual environment before any lint pass runs."""
    return _venv_identity()


@pytest.fixture(scope="module")
def fixture_dir(tmp_path_factory: pytest.TempPathFactory) -> pathlib.Path:
    """Write the lint fixtures outside the repository's lint discovery."""
    directory = tmp_path_factory.mktemp("lint_fixtures")
    for name, source in _FIXTURES.items():
        (directory / name).write_text(textwrap.dedent(source).lstrip(), "utf-8")
    return directory


@pytest.fixture(scope="module")
def pypy_python(venv_before: dict[str, str]) -> str:
    """Provision the pinned PyPy build through the Makefile and return it."""
    del venv_before  # requested only so the snapshot precedes provisioning
    result = run_make("-s", "pylint-pypy-python")
    assert result.returncode == 0, f"provisioning PyPy failed:\n{result.stderr}"
    path = pathlib.Path(result.stdout.strip().splitlines()[-1])
    assert path.is_file(), f"the provisioned interpreter must exist: {path}"
    return str(path)


def _classic_tool(
    pypy_python: str, home: pathlib.Path
) -> tuple[dict[str, str], list[str]]:
    """Return the Makefile's classic-pass tool command, bound to PyPy."""
    assignments, argv = split_command(make_expand("$(PYLINT_TOOL)"))
    env = tool_environment() | assignments | {"PYLINTHOME": str(home)}
    return env, [*argv, "--python", pypy_python]


def _df12_tool(home: pathlib.Path) -> tuple[dict[str, str], list[str]]:
    """Return the Makefile's df12-pass tool command."""
    assignments, argv = split_command(make_expand("$(DF12_TOOL)"))
    return tool_environment() | assignments | {"PYLINTHOME": str(home)}, argv


@pytest.fixture(scope="module")
def classic_run(
    pypy_python: str,
    fixture_dir: pathlib.Path,
    tmp_path_factory: pytest.TempPathFactory,
) -> CompletedRun:
    """Run the classic pass over every fixture with the effective config."""
    env, argv = _classic_tool(pypy_python, tmp_path_factory.mktemp("pylinthome"))
    return _run_tool(
        env,
        argv,
        "pylint",
        "--rcfile=pyproject.toml",
        "--jobs=1",
        _MSG_TEMPLATE,
        *(str(fixture_dir / name) for name in sorted(_FIXTURES)),
    )


@pytest.fixture(scope="module")
def df12_run(
    venv_before: dict[str, str],
    fixture_dir: pathlib.Path,
    tmp_path_factory: pytest.TempPathFactory,
) -> CompletedRun:
    """Run the df12 pass over every fixture with its dedicated config."""
    del venv_before  # requested only so the snapshot precedes this pass
    env, argv = _df12_tool(tmp_path_factory.mktemp("pylinthome"))
    return _run_tool(
        env,
        argv,
        "pylint",
        "--rcfile=pylintrc-df12.toml",
        "--jobs=1",
        _MSG_TEMPLATE,
        *(str(fixture_dir / name) for name in sorted(_FIXTURES)),
    )


def _runtime_check_args(variable: str) -> list[str]:
    """Return the Makefile's arguments for tools/check_lint_runtime.py."""
    return split_command(make_expand(f"$({variable})"))[1]


# 1. Runtime identity and dependency resolution inside each tool environment.


def test_classic_environment_is_pypy_8_on_python_312(
    pypy_python: str, tmp_path: pathlib.Path
) -> None:
    """The classic pass runs vanilla Pylint on PyPy 8.0.0's Python 3.12 build."""
    env, argv = _classic_tool(pypy_python, tmp_path)
    result = _run_tool(
        env,
        argv,
        "python",
        "tools/check_lint_runtime.py",
        *_runtime_check_args("PYLINT_RUNTIME_CHECK"),
    )

    assert result.returncode == 0, f"runtime check failed:\n{result.stderr}"
    record = result.stdout
    for fragment in (
        "implementation=pypy",
        f"pypy={makefile_pin('PYPY_RELEASE')}",
        f"version={makefile_pin('PYPY_PYTHON_VERSION')}",
        f"pylint={makefile_pin('PYLINT_VERSION')}",
        f"astroid={makefile_pin('ASTROID_VERSION')}",
    ):
        assert fragment in record, f"runtime record must show {fragment}:\n{record}"


def test_df12_environment_is_cpython_314(tmp_path: pathlib.Path) -> None:
    """The df12 pass runs on CPython 3.14 with the plugin installed."""
    env, argv = _df12_tool(tmp_path)
    result = _run_tool(
        env,
        argv,
        "python",
        "tools/check_lint_runtime.py",
        *_runtime_check_args("DF12_RUNTIME_CHECK"),
    )

    assert result.returncode == 0, f"runtime check failed:\n{result.stderr}"
    for fragment in (
        "implementation=cpython",
        "version=3.14.",
        f"df12-python-lints={makefile_pin('DF12_PYTHON_LINTS_VERSION')}",
    ):
        assert fragment in result.stdout, (
            f"runtime record must show {fragment}:\n{result.stdout}"
        )


def test_overriding_the_interpreter_fails_before_linting() -> None:
    """An overridden, non-PyPy interpreter is rejected before Pylint runs."""
    env = tool_environment()
    cpython = _run_tool(env, ["uv"], "python", "find", "cpython@3.14")
    assert cpython.returncode == 0, f"CPython 3.14 must be findable:\n{cpython.stderr}"

    result = run_make("lint-pylint", f"PYLINT_PYTHON={cpython.stdout.strip()}")

    assert result.returncode != 0, "a CPython interpreter must fail the classic pass"
    assert "implementation is 'cpython', expected 'pypy'" in result.stderr, (
        f"the mismatch must be reported clearly:\n{result.stderr}"
    )
    assert "rated at" not in result.stdout, "Pylint must not run after a mismatch"


def test_unsatisfiable_dependency_pin_fails_clearly() -> None:
    """Pylint 4.0.8 cannot resolve with Astroid 4.3.1, and the pass says so.

    Pylint 4.0.8 declares ``astroid<=4.1.dev0``, which is why the Makefile
    pins Astroid 4.0.4 and defers 4.3.1.
    """
    result = run_make("lint-pylint", "ASTROID_VERSION=4.3.1")

    assert result.returncode != 0, "an unsatisfiable pin must fail the pass"
    assert "No solution found" in result.stderr, (
        f"uv must explain the unresolvable requirement:\n{result.stderr}"
    )
    assert "rated at" not in result.stdout, "Pylint must not run without its deps"


# 2 and 5. Astroid on PyPy: Python 3.12 syntax and live-object inspection.


def test_astroid_on_pypy_builds_pep695_nodes_and_inspects_builtins(
    pypy_python: str, fixture_dir: pathlib.Path, tmp_path: pathlib.Path
) -> None:
    """Astroid parses PEP 695 syntax and inspects live objects on PyPy.

    PyPy 7.3.22 raised on ``types.FunctionType.__text_signature__`` and broke
    Astroid's bootstrap (pypy/pypy#5458); pylint-pypy-shim existed to paper
    over that and PyPy's ``__class_getitem__`` descriptors. This runs vanilla
    Astroid with no shim and checks it produces useful results.
    """
    env, argv = _classic_tool(pypy_python, tmp_path)
    result = _run_tool(
        env,
        argv,
        "python",
        "-c",
        _ASTROID_PROBE,
        str(fixture_dir / "pep695_violation.py"),
    )

    assert result.returncode == 0, f"the Astroid probe crashed:\n{result.stderr}"
    report = json.loads(result.stdout)
    assert report["implementation"] == "pypy", "the probe must run on PyPy"
    assert report["alias"] == ["Pair", ["T"]], "type Pair[T] must build a TypeAlias"
    assert report["function"] == ["first", ["T"]], "def first[T] must keep its TypeVar"
    assert report["class"] == ["Box", ["T"]], "class Box[T] must keep its TypeVar"
    assert report["text_signature"].startswith("("), (
        "FunctionType.__text_signature__ must be a signature string on PyPy 8"
    )
    assert report["builtins_locals"] > 100, "Astroid must bootstrap builtins fully"
    assert report["list"] == ["ClassDef", True], (
        "builtins.list must build as a class carrying __class_getitem__"
    )
    assert report["list_subscript"] == ["builtins.list"], "list[int] must infer"
    assert report["dict_subscript"] == ["builtins.dict"], "dict[str, int] must infer"


# 3 and 4. The classic pass analyses 3.12 syntax and fails on broken input.


def test_classic_pass_analyses_python_312_syntax(
    classic_run: CompletedRun,
) -> None:
    """A classic check fires in a module that also uses Python 3.12 syntax."""
    findings = _findings(classic_run.stdout)

    assert ("pep695_violation.py", "R1705") in findings, (
        "no-else-return must fire, proving the module was analysed:\n"
        f"{classic_run.stdout}"
    )
    assert ("pep695_violation.py", "E0001") not in findings, (
        "PEP 695 syntax must parse on PyPy 3.12"
    )
    assert classic_run.returncode != 0, "a classic finding must fail the pass"


def test_classic_pass_fails_on_a_syntax_error(
    classic_run: CompletedRun,
) -> None:
    """Pylint itself reports an unparsable module and exits non-zero.

    Pylint runs directly here, so Ruff cannot mask a fail-open Pylint pass.
    """
    assert ("broken.py", "E0001") in _findings(classic_run.stdout), (
        f"the classic config must report syntax errors:\n{classic_run.stdout}"
    )
    assert classic_run.returncode != 0, "a syntax error must fail the pass"


@pytest.mark.parametrize(
    ("target", "fixture", "message_id"),
    [
        ("lint-pylint", "broken.py", "E0001"),
        ("lint-df12", "bare_assert.py", "C9102"),
    ],
    ids=["classic-syntax-error", "df12-bare-assert"],
)
def test_make_propagates_a_pylint_failure(
    fixture_dir: pathlib.Path, target: str, fixture: str, message_id: str
) -> None:
    """Each Make entry point fails when its Pylint pass finds a problem."""
    result = run_make(target, f"PYLINT_TARGETS={fixture_dir / fixture}")

    assert message_id in result.stdout, f"{target} must report {message_id}"
    assert result.returncode != 0, f"make {target} must exit non-zero"


# 6. The df12 pass: its own diagnostics, baseline semantics, and isolation.


def test_df12_pass_reports_its_diagnostics(
    df12_run: CompletedRun,
) -> None:
    """The df12 plugin loads on CPython 3.14 and its checks fail the pass."""
    findings = _findings(df12_run.stdout)

    assert ("bare_assert.py", "C9102") in findings, (
        f"assert-missing-message must fire:\n{df12_run.stdout}"
    )
    assert ("broken.py", "E0001") in findings, "the df12 config must report syntax"
    assert df12_run.returncode != 0, "a df12 finding must fail the pass"


def test_classic_pass_does_not_load_the_df12_plugin(
    classic_run: CompletedRun,
) -> None:
    """The PyPy pass never registers df12 messages."""
    assert ("bare_assert.py", "C9102") not in _findings(classic_run.stdout), (
        "the classic pass must not run the df12 plugin"
    )


def test_df12_pass_keeps_the_312_baseline_on_cpython_314(
    df12_run: CompletedRun,
    fixture_dir: pathlib.Path,
    tmp_path: pathlib.Path,
) -> None:
    """Running on 3.14 does not impose a 3.14 baseline on the code.

    redundant-future-annotations (C9112) reads ``py-version``: with the
    project's 3.12 baseline, required ``from __future__ import annotations``
    imports stay. The control run at a 3.14 baseline proves the check is live.
    """
    fixture = str(fixture_dir / "future_annotations.py")
    assert ("future_annotations.py", "C9112") not in _findings(df12_run.stdout), (
        "baseline-3.12 code must keep its __future__ import"
    )

    env, argv = _df12_tool(tmp_path)
    control = _run_tool(
        env,
        argv,
        "pylint",
        "--rcfile=pylintrc-df12.toml",
        "--py-version=3.14",
        _MSG_TEMPLATE,
        fixture,
    )
    assert ("future_annotations.py", "C9112") in _findings(control.stdout), (
        f"C9112 must fire at a 3.14 baseline, proving it is enabled:\n{control.stdout}"
    )


def test_provisioning_keeps_pypy_off_path(tmp_path: pathlib.Path) -> None:
    """Installing PyPy never places an executable in uv's bin directory."""
    bin_dir = tmp_path / "bin"
    env = tool_environment() | {"UV_PYTHON_BIN_DIR": str(bin_dir)}

    result = run_make(
        "-s", "pylint-pypy-python", f"PYPY_INSTALL_DIR={tmp_path / 'python'}", env=env
    )

    assert result.returncode == 0, f"provisioning failed:\n{result.stderr}"
    installed = list(bin_dir.iterdir()) if bin_dir.exists() else []
    assert not installed, f"--no-bin must keep PyPy off PATH, found {installed}"


def test_project_venv_is_untouched(
    venv_before: dict[str, str],
    classic_run: CompletedRun,
    df12_run: CompletedRun,
) -> None:
    """Neither pass replaces or re-creates the project virtual environment.

    The project's own interpreter version is not asserted: it follows
    ``UV_PYTHON`` (CI pins 3.13; an unset local value lets uv choose), and may
    legitimately be 3.14 locally. What linting must never do is change it.
    """
    del classic_run, df12_run  # requested only so both passes run first
    after = _venv_identity()

    assert after == venv_before, "the project .venv must be unchanged by linting"
    assert "pypy" not in after["pyvenv.cfg"].lower(), (
        "the project .venv must never become the PyPy lint interpreter"
    )
