"""Contracts for the isolated build inputs used to build release artefacts.

The release wheel is produced by an isolated PEP 517 build environment, which
never reads ``uv.lock``. These contracts keep that environment on a reviewed,
hash-verified set: exact backend pins, a generated constraints file covering
every transitive build requirement, and recipes that refuse an unhashed or
cached resolution.
"""

from __future__ import annotations

import dataclasses as dc
import re
import tomllib

import pytest
from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import canonicalize_name

from tests._makefile import (
    REPO_ROOT,
    CompletedRun,
    makefile_recipe,
    run_command,
    tool_environment,
)

PYPROJECT = REPO_ROOT / "pyproject.toml"
CONSTRAINTS = REPO_ROOT / "build-constraints.txt"

_SHA256_HEX = re.compile(r"[0-9a-f]{64}")
_HASH_OPTION = re.compile(r"--hash=(?P<algorithm>[A-Za-z0-9]+):(?P<digest>\S+)")


class BuildInputError(AssertionError):
    """An isolated build input is not the reviewed, integrity-bound form."""


class ExactPinRequiredError(BuildInputError):
    """A build requirement is not a single exact ``==`` pin."""

    def __init__(self, subject: str) -> None:
        """Build the exception message."""
        super().__init__(f"{subject!r} must be one exact == pin")


class WildcardPinRefusedError(BuildInputError):
    """A wildcard pin could still select an unreviewed release."""

    def __init__(self, subject: str) -> None:
        """Build the exception message."""
        super().__init__(f"{subject!r} must not use a wildcard pin")


class UnrecognizedHashOptionError(BuildInputError):
    """A constraint carries an option that is not a hash."""

    def __init__(self, subject: str, option: str) -> None:
        """Build the exception message."""
        super().__init__(f"{subject}: unrecognized option {option!r}")


class NonSha256HashError(BuildInputError):
    """A constraint uses a hash algorithm other than SHA-256."""

    def __init__(self, subject: str) -> None:
        """Build the exception message."""
        super().__init__(f"{subject}: only sha256 hashes are accepted")


class MalformedHashError(BuildInputError):
    """A constraint's SHA-256 digest is not 64 hexadecimal characters."""

    def __init__(self, subject: str, digest: str) -> None:
        """Build the exception message."""
        super().__init__(f"{subject}: malformed sha256 hash {digest!r}")


class MissingHashError(BuildInputError):
    """A constraint pins no distribution digest."""

    def __init__(self, subject: str) -> None:
        """Build the exception message."""
        super().__init__(f"{subject}: needs at least one --hash=sha256 value")


class InvalidConstraintRequirementError(BuildInputError):
    """A constraints line is not a valid requirement."""

    def __init__(self, requirement: str) -> None:
        """Build the exception message."""
        super().__init__(f"invalid requirement {requirement!r}")


class DuplicateConstraintError(BuildInputError):
    """The constraints file pins the same project twice."""

    def __init__(self, name: str) -> None:
        """Build the exception message."""
        super().__init__(f"duplicate constraint {name}")


class EmptyConstraintsError(BuildInputError):
    """The constraints file declares no requirements at all."""

    def __init__(self) -> None:
        """Build the exception message."""
        super().__init__("build-constraints.txt declares no requirements")


class MissingConstraintError(BuildInputError):
    """A backend requirement has no entry in the constraints file."""

    def __init__(self, name: str) -> None:
        """Build the exception message."""
        super().__init__(f"build-constraints.txt has no entry for {name}")


class ConstraintVersionMismatchError(BuildInputError):
    """The constraints file pins a different version from the backend pin."""

    def __init__(self, name: str, pinned: str, required: str) -> None:
        """Build the exception message."""
        super().__init__(
            f"build-constraints.txt pins {name}=={pinned}, "
            f"but pyproject.toml requires {name}=={required}"
        )


@dc.dataclass(frozen=True, slots=True)
class Constraint:
    """One pinned build requirement and its verified distribution hashes.

    Attributes
    ----------
    name : str
        The normalized project name.
    version : str
        The exact version the constraints file pins.
    hashes : tuple of str
        Every lowercase SHA-256 digest the file permits for the distribution.
    """

    name: str
    version: str
    hashes: tuple[str, ...]


def _git(*arguments: str) -> CompletedRun:
    """Run a read-only ``git`` query in the repository root.

    Parameters
    ----------
    *arguments : str
        The arguments after ``git``.

    Returns
    -------
    CompletedRun
        The finished process with its captured output.
    """
    return run_command(["git", *arguments], env=tool_environment())


def exact_version(requirement: Requirement, subject: str) -> str:
    """Return the version of an exact ``==`` requirement.

    Parameters
    ----------
    requirement : packaging.requirements.Requirement
        The parsed requirement.
    subject : str
        The requirement text, named in a failure.

    Returns
    -------
    str
        The pinned version.

    Raises
    ------
    ExactPinRequiredError
        If the requirement is not a single exact ``==`` pin.
    WildcardPinRefusedError
        If the pin uses a wildcard that can still select an unreviewed
        release.
    """
    specifiers = list(requirement.specifier)
    if len(specifiers) != 1 or specifiers[0].operator != "==":
        raise ExactPinRequiredError(subject)
    version = specifiers[0].version
    if "*" in version:
        raise WildcardPinRefusedError(subject)
    return version


def build_requirements(text: str) -> list[Requirement]:
    """Parse the ``[build-system].requires`` entries from *text*.

    Parameters
    ----------
    text : str
        The contents of ``pyproject.toml``.

    Returns
    -------
    list of packaging.requirements.Requirement
        One parsed requirement per declared build input.
    """
    data = tomllib.loads(text)
    return [Requirement(item) for item in data["build-system"]["requires"]]


def _logical_lines(text: str) -> list[str]:
    """Join uv's backslash continuations into one line per requirement.

    Parameters
    ----------
    text : str
        The contents of a uv-generated constraints file.

    Returns
    -------
    list of str
        Each requirement with its hash options on one line.
    """
    blocks: list[str] = []
    pending: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if raw[:1].isspace():
            pending.append(line.removesuffix("\\").strip())
            continue
        if pending:
            blocks.append(" ".join(pending))
        pending = [line.removesuffix("\\").strip()]
    if pending:
        blocks.append(" ".join(pending))
    return blocks


def _hashes(options: list[str], subject: str) -> tuple[str, ...]:
    """Return the verified SHA-256 digests in *options*.

    Parameters
    ----------
    options : list of str
        The whitespace-separated options following a requirement.
    subject : str
        The requirement text, named in a failure.

    Returns
    -------
    tuple of str
        The lowercase hexadecimal digests.

    Raises
    ------
    MalformedHashError
        If a digest is not 64 lowercase hexadecimal characters.
    MissingHashError
        If the requirement carries no hash at all.
    NonSha256HashError
        If an option names a hash algorithm other than SHA-256.
    UnrecognizedHashOptionError
        If an option is not a hash at all.
    """
    hashes: list[str] = []
    for option in options:
        match = _HASH_OPTION.fullmatch(option)
        if match is None:
            raise UnrecognizedHashOptionError(subject, option)
        if match.group("algorithm") != "sha256":
            raise NonSha256HashError(subject)
        digest = match.group("digest")
        if _SHA256_HEX.fullmatch(digest) is None:
            raise MalformedHashError(subject, digest)
        hashes.append(digest)
    if not hashes:
        raise MissingHashError(subject)
    return tuple(hashes)


def parse_constraints(text: str) -> dict[str, Constraint]:
    """Parse a uv-generated constraints file into exact pins with hashes.

    Parameters
    ----------
    text : str
        The contents of ``build-constraints.txt``.

    Returns
    -------
    dict of str to Constraint
        One entry per requirement name.

    Raises
    ------
    DuplicateConstraintError
        If the file pins the same project twice.
    EmptyConstraintsError
        If the file declares no requirements.
    InvalidConstraintRequirementError
        If a line is not a valid requirement.
    """
    constraints: dict[str, Constraint] = {}
    for line in _logical_lines(text):
        requirement_token, *options = line.split()
        try:
            requirement = Requirement(requirement_token)
        except InvalidRequirement as error:
            raise InvalidConstraintRequirementError(requirement_token) from error
        name = canonicalize_name(requirement.name)
        version = exact_version(requirement, requirement_token)
        if name in constraints:
            raise DuplicateConstraintError(name)
        constraints[name] = Constraint(
            name=name,
            version=version,
            hashes=_hashes(options, requirement_token),
        )
    if not constraints:
        raise EmptyConstraintsError
    return constraints


def assert_constraints_cover(
    requirements: list[Requirement], constraints: dict[str, Constraint]
) -> None:
    """Require every backend requirement to appear at the same exact version.

    Parameters
    ----------
    requirements : list of packaging.requirements.Requirement
        The declared ``[build-system].requires`` entries.
    constraints : dict of str to Constraint
        The generated constraints, keyed by project name.

    Raises
    ------
    ConstraintVersionMismatchError
        If the constraints file pins a different version.
    MissingConstraintError
        If a backend requirement has no entry in the file.
    """
    for requirement in requirements:
        version = exact_version(requirement, str(requirement))
        constraint = constraints.get(canonicalize_name(requirement.name))
        if constraint is None:
            raise MissingConstraintError(requirement.name)
        if constraint.version != version:
            raise ConstraintVersionMismatchError(
                requirement.name, constraint.version, version
            )


def test_every_build_requirement_is_an_exact_pin() -> None:
    """A range or unpinned backend could select an unreviewed release."""
    for requirement in build_requirements(PYPROJECT.read_text(encoding="utf-8")):
        exact_version(requirement, str(requirement))


def test_constraints_are_committed_and_not_ignored() -> None:
    """A constraints file nobody tracks protects nobody."""
    assert CONSTRAINTS.is_file(), "build-constraints.txt must exist"
    ignored = _git("check-ignore", "--no-index", "--quiet", "build-constraints.txt")
    assert ignored.returncode == 1, (
        "build-constraints.txt must not be ignored; the release build would "
        "then resolve unreviewed hashes"
    )
    tracked = _git("ls-files", "--error-unmatch", "build-constraints.txt")
    assert tracked.returncode == 0, (
        "build-constraints.txt must be committed; "
        f"`git ls-files --error-unmatch` failed: {tracked.stderr.strip()}"
    )


def test_constraints_cover_every_backend_requirement() -> None:
    """The generated pins must agree with the reviewed backend versions."""
    requirements = build_requirements(PYPROJECT.read_text(encoding="utf-8"))
    constraints = parse_constraints(CONSTRAINTS.read_text(encoding="utf-8"))
    assert_constraints_cover(requirements, constraints)


def test_every_constraint_has_a_sha256_hash() -> None:
    """Every transitive build input needs at least one verified digest."""
    constraints = parse_constraints(CONSTRAINTS.read_text(encoding="utf-8"))
    for constraint in constraints.values():
        assert constraint.hashes, constraint.name
        assert all(_SHA256_HEX.fullmatch(item) for item in constraint.hashes), (
            f"{constraint.name} has a malformed digest"
        )


_REQUIRED_BUILD_OPTIONS = (
    "--build-constraint build-constraints.txt",
    "--require-build-hashes",
    "--no-cache",
)


def assert_build_invocations_are_verified(recipe: str, target: str) -> None:
    """Require every ``uv build`` call to constrain and verify its inputs.

    Parameters
    ----------
    recipe : str
        A Makefile recipe body.
    target : str
        The Make target, named in a failure.

    Notes
    -----
    Each invocation is checked separately, so one unconstrained build cannot
    hide behind two correct ones.
    """
    invocations = recipe.split("$(UV) build")[1:]
    assert invocations, f"{target} must call uv build"
    for index, invocation in enumerate(invocations):
        for option in _REQUIRED_BUILD_OPTIONS:
            assert option in invocation, f"{target} build {index} lacks {option}"


@pytest.mark.parametrize("target", ["release-wheel", "build-release"])
def test_build_recipes_require_verified_isolated_inputs(target: str) -> None:
    """Every release build isolates the backend and verifies its hashes."""
    recipe = makefile_recipe(target)
    assert_build_invocations_are_verified(recipe, target)
    assert "SOURCE_DATE_EPOCH" in recipe, f"{target} must fix the build timestamp"
    assert "--no-build-isolation" not in recipe, f"{target} must isolate builds"


@pytest.mark.parametrize("option", _REQUIRED_BUILD_OPTIONS)
def test_one_unconstrained_build_invocation_is_refused(option: str) -> None:
    """A single invocation missing an option fails the whole recipe."""
    invocation = "$(UV) build " + " ".join(_REQUIRED_BUILD_OPTIONS)
    recipe = " ; ".join([invocation, invocation.replace(option, ""), invocation])
    with pytest.raises(AssertionError, match="build 1 lacks"):
        assert_build_invocations_are_verified(recipe, "synthetic")


def test_release_wheel_compares_two_independent_builds() -> None:
    """The release recipe builds twice and refuses differing digests."""
    recipe = makefile_recipe("release-wheel")
    assert recipe.count("--wheel") >= 2, "the wheel must be built twice"
    missing = [
        token
        for token in ("mktemp -d", "sha256sum", "Wheel digests differ")
        if token not in recipe
    ]
    assert not missing, f"release-wheel lacks {missing}"


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        ("setuptools>=61.0", "exact"),
        ("setuptools==84.2.1", "hash"),
        ("setuptools==84.2.1 \\\n    --hash=sha256:ABC", "malformed"),
        ("setuptools==84.2.1 \\\n    --hash=md5:" + "a" * 32, "sha256"),
    ],
    ids=["range", "missing-hash", "malformed-hash", "wrong-algorithm"],
)
def test_constraint_parser_rejects_unreviewed_inputs(text: str, reason: str) -> None:
    """A range, unhashed or malformed pin must fail the contract."""
    with pytest.raises(BuildInputError, match=reason):
        parse_constraints(text)


def test_constraint_lookup_ignores_project_name_spelling() -> None:
    """PEP 503 normalization makes Setuptools match setuptools."""
    constraints = parse_constraints(
        "setuptools==84.2.1 \\\n    --hash=sha256:" + "a" * 64 + "\n"
    )
    assert_constraints_cover([Requirement("Setuptools==84.2.1")], constraints)


def test_constraints_must_match_the_backend_pin() -> None:
    """A constraints file that pins another version is a review failure."""
    requirements = [Requirement("setuptools==84.2.1")]
    constraints = parse_constraints(
        "setuptools==84.2.0 \\\n    --hash=sha256:" + "a" * 64 + "\n"
    )
    with pytest.raises(BuildInputError, match=r"84\.2\.1"):
        assert_constraints_cover(requirements, constraints)


def test_wildcard_pins_are_rejected() -> None:
    """A wildcard can still select an unreviewed release."""
    with pytest.raises(BuildInputError, match="wildcard"):
        exact_version(Requirement("setuptools==84.*"), "setuptools==84.*")
