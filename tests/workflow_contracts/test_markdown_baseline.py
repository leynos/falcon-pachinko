"""The estate's Markdown formatting and linting baseline.

Six things must hold, and each fails in a way a green run cannot show.

* `fmt` and `check-fmt` exist at all (FP-003).
* A recipe reachable from `check-fmt` runs `mdtablefix --check` over the
  Git-selected set, and its status reaches Make (PD-002).
* A recipe reachable from `fmt` runs `mdtablefix --in-place` over the same set,
  directly rather than through a wrapper (PD-003).
* A recipe reachable from `fmt` runs `markdownlint-cli2 --fix` directly
  (PD-004).
* `.markdownlint-cli2.jsonc` carries the canonical rules and exclusions
  (PD-005).
* CI lints Markdown through the upstream action pinned to a full commit SHA,
  and no `run:` step invokes the linter or drives `make markdownlint` (PD-006).

The last is the one worth stating plainly. A `run:` step resolves the linter
from the npm registry while the gate is running, so a moved tag or a registry
outage changes what the gate enforces without changing this repository. The
action's release carries the linter's whole dependency graph instead.

This module reads the Makefile itself rather than running it, because what is
asserted is which command a recipe names. Variable references are expanded, so
a recipe that calls `$(MDLINT)` is read as the command that variable holds; a
contract matching the literal text would pass on a variable pointing anywhere.

Run with:

    uv run pytest tests/workflow_contracts
"""

from __future__ import annotations

import json
import pathlib
import re
import typing as typ

import pytest
import yaml

if typ.TYPE_CHECKING:
    import collections.abc as cabc

ROOT = pathlib.Path(__file__).resolve().parents[2]
MAKEFILE = ROOT / "Makefile"
MARKDOWN_CONFIG = ROOT / ".markdownlint-cli2.jsonc"
WORKFLOW_DIR = ROOT / ".github" / "workflows"

#: Targets the baseline requires to exist.
REQUIRED_TARGETS = ("fmt", "check-fmt")
#: The flags that select the Markdown set: tracked files, plus new ones.
SELECT_FLAGS = ("--git", "--include-untracked")
#: The upstream action, and the only way CI may lint Markdown.
LINT_ACTION = "DavidAnson/markdownlint-cli2-action"
#: The action that provisions the table formatter. `check-fmt` now requires
#: `mdtablefix`, so the lane running it must install it.
MDTABLEFIX_ACTION = "install-mdtablefix"
FULL_SHA = re.compile(r"[0-9a-f]{40}")

#: The canonical rule configuration, from the estate canon. A repository may
#: add rules alongside these; it may not weaken one.
CANONICAL_CONFIG: typ.Final[dict[str, object]] = {
    "MD004": {"style": "dash"},
    "MD010": {"code_blocks": False},
    "MD013": {
        "line_length": 80,
        "code_block_line_length": 120,
        "tables": False,
        "headings": False,
    },
    "MD029": {"style": "ordered"},
}
#: The canonical exclusions. A repository may add globs alongside these.
CANONICAL_IGNORES: typ.Final[tuple[str, ...]] = (
    "**/.venv/**",
    ".vtcode/**",
    "**/node_modules/**",
    "**/target/**",
    ".terraform/**",
    ".uv-cache/**",
    "memories/**",
    "CRUSH.md",
)

_ASSIGNMENT = re.compile(r"^(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s*[:?]?=\s*(?P<value>.*)$")
_TARGET = re.compile(r"^(?P<targets>[^\t#=][^:=]*):(?!=)\s*(?P<prerequisites>.*)$")


def _assignments(lines: list[str]) -> dict[str, str]:
    """Return the Makefile's simple variable assignments.

    Parameters
    ----------
    lines : list[str]
        The Makefile's logical lines, continuations already joined.

    Returns
    -------
    dict[str, str]
        Variable name to its declared value.
    """
    found: dict[str, str] = {}
    for line in lines:
        if line.startswith("\t"):
            continue
        match = _ASSIGNMENT.match(line)
        if match:
            found[match["name"]] = match["value"].strip()
    return found


def _expand(text: str, variables: dict[str, str], depth: int = 0) -> str:
    """Expand ``$(NAME)`` references against *variables*.

    A reference to an undefined variable expands to nothing, as Make does.
    Recursion is bounded so a self-referential definition cannot hang the
    suite.

    Parameters
    ----------
    text : str
        The text to expand.
    variables : dict[str, str]
        The Makefile's assignments.
    depth : int
        Current recursion depth.

    Returns
    -------
    str
        The expanded text.
    """
    if depth > 8 or "$(" not in text:
        return text
    expanded = re.sub(
        r"\$\((?P<name>[A-Za-z_][A-Za-z0-9_]*)\)",
        lambda match: variables.get(match["name"], ""),
        text,
    )
    return _expand(expanded, variables, depth + 1)


def _logical_lines() -> list[str]:
    """Return the Makefile with backslash continuations joined.

    Returns
    -------
    list[str]
        One entry per logical line.
    """
    raw = MAKEFILE.read_text(encoding="utf-8").split("\n")
    joined: list[str] = []
    for line in raw:
        if joined and joined[-1].endswith("\\"):
            joined[-1] = joined[-1][:-1].rstrip() + " " + line.strip()
        else:
            joined.append(line)
    return joined


@pytest.fixture(scope="module")
def makefile() -> tuple[dict[str, list[str]], dict[str, str]]:
    """Return the Makefile's recipes and variables.

    Returns
    -------
    tuple[dict[str, list[str]], dict[str, str]]
        Target name to its recipe lines, and the variable assignments.
    """
    lines = _logical_lines()
    variables = _assignments(lines)
    return _recipes(lines, variables), variables


def _recipes(lines: list[str], variables: dict[str, str]) -> dict[str, list[str]]:
    """Return each target's recipe, with variable references expanded.

    Parameters
    ----------
    lines : list[str]
        The Makefile's logical lines.
    variables : dict[str, str]
        The Makefile's assignments.

    Returns
    -------
    dict[str, list[str]]
        Target name to its recipe lines. Targets sharing a rule share one
        list, as Make gives them one recipe.
    """
    recipes: dict[str, list[str]] = {}
    current: list[str] = []
    for line in lines:
        if line.startswith("\t"):
            current.append(_expand(line.lstrip("\t"), variables))
        elif match := _TARGET.match(line):
            current = []
            recipes.update(_named(match["targets"], variables, current))
    return recipes


def _named(
    targets: str, variables: dict[str, str], recipe_lines: list[str]
) -> dict[str, list[str]]:
    """Map each name in a rule's target list to its recipe.

    Parameters
    ----------
    targets : str
        The rule's target field.
    variables : dict[str, str]
        The Makefile's assignments.
    recipe_lines : list[str]
        The list the rule's recipe lines will be appended to.

    Returns
    -------
    dict[str, list[str]]
        Each expanded target name mapped to *recipe_lines*.
    """
    return {_expand(target, variables): recipe_lines for target in targets.split()}


def recipe(makefile: tuple[dict[str, list[str]], dict[str, str]], name: str) -> str:
    """Return one target's recipe as text.

    Parameters
    ----------
    makefile : tuple[dict[str, list[str]], dict[str, str]]
        The parsed Makefile.
    name : str
        The target name.

    Returns
    -------
    str
        The recipe's lines joined by newlines.
    """
    recipes, _ = makefile
    assert name in recipes, (
        f"the Makefile must define {name!r}; it has {sorted(recipes)}"
    )
    return "\n".join(recipes[name])


def _command_lines(text: str, command: str) -> list[str]:
    """Return the recipe lines invoking *command*.

    Parameters
    ----------
    text : str
        A recipe.
    command : str
        The command to look for.

    Returns
    -------
    list[str]
        The matching lines.
    """
    return [line for line in text.split("\n") if command in line]


def _workflow_documents() -> dict[str, dict[object, object]]:
    """Return every workflow, parsed.

    Returns
    -------
    dict[str, dict[object, object]]
        File name to parsed document.
    """
    found = {}
    for path in sorted(WORKFLOW_DIR.iterdir()):
        if path.suffix not in {".yml", ".yaml"}:
            continue
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
        if isinstance(document, dict):
            found[path.name] = document
    return found


def _steps(document: dict[object, object]) -> cabc.Iterator[dict[str, object]]:
    """Yield every step of every job in one workflow.

    A job calling a reusable workflow declares no steps.

    Yields
    ------
    dict[str, object]
        Each declared step.
    """
    jobs = document.get("jobs")
    for job in jobs.values() if isinstance(jobs, dict) else ():
        declared = job.get("steps") if isinstance(job, dict) else None
        if not isinstance(declared, list):
            continue
        yield from (step for step in declared if isinstance(step, dict))


def _job_steps_by_job(
    document: dict[object, object],
) -> dict[str, list[dict[str, object]]]:
    """Return each job's steps, keyed by job name and in declaration order.

    Parameters
    ----------
    document : dict[object, object]
        A parsed workflow.

    Returns
    -------
    dict[str, list[dict[str, object]]]
        Job name to its steps.
    """
    jobs = document.get("jobs")
    found: dict[str, list[dict[str, object]]] = {}
    for name, job in jobs.items() if isinstance(jobs, dict) else ():
        declared = job.get("steps") if isinstance(job, dict) else None
        if isinstance(declared, list):
            found[str(name)] = [step for step in declared if isinstance(step, dict)]
    return found


def _every_job_scoped(document: dict[object, object]) -> bool:
    """Say whether every job in a workflow declares its own permissions.

    A workflow may scope the token at workflow level or on each job. Both
    satisfy the rule; declaring it nowhere does not.

    Parameters
    ----------
    document : dict[object, object]
        A parsed workflow.

    Returns
    -------
    bool
        True when every declared job carries a permissions mapping.
    """
    jobs = document.get("jobs")
    if not isinstance(jobs, dict):
        return False
    return all(
        isinstance(job.get("permissions"), dict)
        for job in jobs.values()
        if isinstance(job, dict)
    )


def _pull_request_workflows() -> dict[str, dict[object, object]]:
    """Return every workflow a pull request's head can reach.

    Returns
    -------
    dict[str, dict[object, object]]
        File name to parsed document.
    """
    reachable = {}
    for name, document in _workflow_documents().items():
        # YAML 1.1 reads the bare word `on` as the boolean True, so a reader
        # asking only for the string finds nothing and clears every workflow.
        triggers = document.get("on", document.get(True))
        if isinstance(triggers, dict) and {"pull_request", "pull_request_target"} & set(
            map(str, triggers)
        ):
            reachable[name] = document
    return reachable


class TestMakefileBaseline:
    """What `fmt` and `check-fmt` name, and which linter the Makefile means."""

    @pytest.mark.parametrize("target", REQUIRED_TARGETS)
    def test_the_required_targets_exist(
        self, makefile: tuple[dict[str, list[str]], dict[str, str]], target: str
    ) -> None:
        """FP-003: the baseline's entry points exist.

        Every other rule here names a recipe reachable from one of these, so a
        missing target would make those rules unaskable rather than failing.
        """
        recipes, _ = makefile

        assert target in recipes, (
            f"the Makefile must define {target!r}; it defines {sorted(recipes)}"
        )

    def test_check_fmt_verifies_the_markdown_tables(
        self,
        makefile: tuple[dict[str, list[str]], dict[str, str]],
    ) -> None:
        """PD-002: `check-fmt` checks, and its verdict reaches Make.

        A recipe line prefixed with `-` tells Make to ignore the command's status,
        which turns a formatting gate into a formatting report.
        """
        lines = _command_lines(recipe(makefile, "check-fmt"), "mdtablefix")

        assert lines, "check-fmt must run mdtablefix"
        checking = [line for line in lines if "--check" in line]
        assert checking, f"check-fmt must run mdtablefix --check; it runs {lines}"
        for line in checking:
            for flag in SELECT_FLAGS:
                assert flag in line, (
                    f"check-fmt must select the Markdown set with {flag}; it "
                    f"runs {line!r}"
                )
            assert not line.lstrip().startswith("-"), (
                f"check-fmt must let mdtablefix's status reach Make; it runs {line!r}"
            )

    def test_fmt_rewrites_the_markdown_tables(
        self,
        makefile: tuple[dict[str, list[str]], dict[str, str]],
    ) -> None:
        """PD-003: `fmt` rewrites, directly rather than through a wrapper.

        A wrapper is a second place the select flags and the tool version can
        drift, and the estate has one such wrapper already, `mdformat-all`, whose
        behaviour this repository could not see from here.
        """
        text = recipe(makefile, "fmt")
        lines = _command_lines(text, "mdtablefix")

        assert lines, "fmt must run mdtablefix"
        assert "mdformat-all" not in text, (
            "fmt must call mdtablefix directly, not through mdformat-all"
        )
        rewriting = [line for line in lines if "--in-place" in line]
        assert rewriting, f"fmt must run mdtablefix --in-place; it runs {lines}"
        for line in rewriting:
            for flag in SELECT_FLAGS:
                assert flag in line, (
                    f"fmt must select the Markdown set with {flag}; it runs {line!r}"
                )

    def test_fmt_applies_the_linter_s_own_fixes(
        self,
        makefile: tuple[dict[str, list[str]], dict[str, str]],
    ) -> None:
        """PD-004: `fmt` runs the linter's fixer directly.

        Without it a contributor formats tables, pushes, and learns from CI that
        the linter wanted something else. The two tools fix different things and
        both belong in one command.
        """
        text = recipe(makefile, "fmt")
        lines = _command_lines(text, "markdownlint-cli2")

        assert lines, f"fmt must run markdownlint-cli2; its recipe is {text!r}"
        assert any("--fix" in line for line in lines), (
            f"fmt must run markdownlint-cli2 --fix; it runs {lines}"
        )
        assert "mdformat-all" not in text, (
            "fmt must call the linter directly, not through mdformat-all"
        )

    def test_the_makefile_names_the_linter_that_exists(
        self,
        makefile: tuple[dict[str, list[str]], dict[str, str]],
    ) -> None:
        """Name `markdownlint-cli2`, which is a different program from `markdownlint`.

        This repository named the latter. It resolved to nothing, so the Markdown
        target ran `xargs` with no command and exited zero having linted nothing:
        a gate that reported success while checking the empty set.
        """
        _, variables = makefile
        declared = _expand(variables.get("MDLINT", ""), variables)

        assert declared, "the Makefile must name a Markdown linter"
        assert declared.rstrip("/").endswith("markdownlint-cli2"), (
            f"the linter is markdownlint-cli2, not {declared!r}; markdownlint is a "
            "different program and resolving to neither lints nothing"
        )


class TestWorkflowBaseline:
    """The configuration, and how CI lints and provisions Markdown."""

    def test_the_markdown_configuration_is_the_canonical_one(self) -> None:
        """PD-005: the rules and exclusions are the estate's.

        An alternate file name does not satisfy the check, because
        `markdownlint-cli2` reads several and a repository carrying two would
        enforce whichever it happened to find first.
        """
        assert MARKDOWN_CONFIG.is_file(), (
            f"{MARKDOWN_CONFIG.name} must exist; an alternate configuration file "
            "name does not satisfy the baseline"
        )
        document = json.loads(
            re.sub(
                r"^\s*//.*$",
                "",
                MARKDOWN_CONFIG.read_text(encoding="utf-8"),
                flags=re.MULTILINE,
            )
        )
        config = document.get("config")
        ignores = document.get("ignores")

        assert isinstance(config, dict), f"{MARKDOWN_CONFIG.name} must declare config"
        assert isinstance(ignores, list), f"{MARKDOWN_CONFIG.name} must declare ignores"
        for rule, expected in CANONICAL_CONFIG.items():
            assert config.get(rule) == expected, (
                f"{rule} must carry the canonical settings {expected!r}; it carries "
                f"{config.get(rule)!r}"
            )
        missing = [glob for glob in CANONICAL_IGNORES if glob not in ignores]
        assert not missing, (
            f"the canonical exclusions must all be present; missing {missing}"
        )

    def test_ci_lints_markdown_through_the_pinned_action(self) -> None:
        """PD-006: one action step, pinned, over every Markdown file.

        The action's release carries the linter's whole dependency graph, so
        nothing is resolved from the registry while the gate runs. A checkout with
        no action step at all is noncompliant: this repository installed the
        linter in CI and then linted nothing with it.
        """
        linting = [
            step
            for document in _workflow_documents().values()
            for step in _steps(document)
            if LINT_ACTION in str(step.get("uses", ""))
        ]

        assert linting, f"CI must lint Markdown through {LINT_ACTION}"
        for step in linting:
            reference = str(step["uses"])
            digest = reference.rsplit("@", 1)[-1]
            assert FULL_SHA.fullmatch(digest), (
                f"{LINT_ACTION} must be pinned to a full commit SHA; got {reference!r}"
            )
            inputs = step.get("with")
            assert isinstance(inputs, dict), f"{reference} must declare inputs"
            assert inputs.get("globs") == "**/*.md", (
                f"{reference} must lint every Markdown file; it declares "
                f"globs={inputs.get('globs')!r}"
            )

    def test_ci_installs_the_table_formatter_before_it_runs_the_gate(self) -> None:
        """Provision `mdtablefix` in the same job, before the gate that needs it.

        `check-fmt` names `mdtablefix` as a prerequisite, so on a runner without it
        the target fails before reading a single file. Two weaker readings both
        look like compliance and are not: a tool installed in a *different* job is
        not on this job's runner at all, since jobs do not share a filesystem; and
        a tool installed *after* the gate step has not been installed when the gate
        runs. A flattened search over a workflow's steps accepts both.
        """
        checked = []
        for name, document in _workflow_documents().items():
            for job_name, steps in _job_steps_by_job(document).items():
                gates = [
                    index
                    for index, step in enumerate(steps)
                    if "check-fmt" in str(step.get("run", ""))
                ]
                if not gates:
                    continue
                installs = [
                    index
                    for index, step in enumerate(steps)
                    if MDTABLEFIX_ACTION in str(step.get("uses", ""))
                ]
                where = f"{name}:{job_name}"
                checked.append(where)
                assert installs, (
                    f"{where} runs make check-fmt, which requires mdtablefix, and "
                    "installs it nowhere in that job; jobs do not share a runner"
                )
                assert min(installs) < min(gates), (
                    f"{where} installs mdtablefix after it runs make check-fmt; "
                    f"install at step {min(installs)}, gate at step {min(gates)}"
                )
                for index in installs:
                    reference = str(steps[index]["uses"])
                    assert FULL_SHA.fullmatch(reference.rsplit("@", 1)[-1]), (
                        f"{reference} must be pinned to a full commit SHA"
                    )

        assert checked, "some job must run make check-fmt"

    def test_the_pull_request_token_is_scoped(self) -> None:
        """Give every lane a pull request can reach a least-privilege token.

        Without a `permissions` block a workflow's steps take the repository
        default, and every third-party action the lane calls receives it. This lane
        calls two, and it runs on a pull request, which is where the head is not
        ours. Declared at workflow level so a job added later inherits the scope
        rather than silently taking the default.

        Scoped to pull-request-reachable workflows deliberately. A `workflow_call`
        file inherits its caller's permissions, so declaring a block there would
        narrow what a caller intended rather than tightening anything, and
        `build-wheels.yml` is such a file.
        """
        reachable = _pull_request_workflows()
        unscoped = sorted(
            name
            for name, document in reachable.items()
            if not isinstance(document.get("permissions"), dict)
            and not _every_job_scoped(document)
        )

        assert reachable, "some workflow must serve pull requests"
        assert not unscoped, (
            "every workflow a pull request can reach must declare a permissions "
            f"block, at workflow level or on every job; these declare none: "
            f"{unscoped}"
        )

    def test_no_ci_step_runs_the_linter_itself(self) -> None:
        """PD-006's other half: no `run:` step may lint or install the linter.

        A `run:` step resolving `markdownlint-cli2` from the registry makes the
        gate depend on what the registry serves that minute. Driving
        `make markdownlint` is the same defect wearing a Makefile.
        """
        offending = [
            f"{name}: {step.get('name', step.get('run', ''))!r}"
            for name, document in _workflow_documents().items()
            for step in _steps(document)
            if "markdownlint" in str(step.get("run", ""))
        ]

        assert not offending, (
            "CI must lint Markdown only through the pinned action; these steps "
            f"invoke or install the linter themselves: {offending}"
        )
