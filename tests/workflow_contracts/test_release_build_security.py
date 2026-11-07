"""Contracts for release build authority and distribution identity.

The release build runs a resolved PEP 517 backend. These contracts keep that
job read-only, keep publication write-scoped jobs from rebuilding, and require
the publication path to verify the digest the build job reported before it
uploads anything.
"""

from __future__ import annotations

import re
import typing as typ

import pytest

from tests.helpers.ci_workflows import ROOT, job, jobs, steps, workflow_document
from tests.helpers.strict_yaml import load

RELEASE = "release.yml"
CI = "ci.yml"
WHEEL_ACTION = ROOT / ".github" / "actions" / "pure-python-wheel" / "action.yml"

_BUILD_COMMAND = re.compile(r"(?<![\w-])(?:uv|make)(?![\w-])")
_EXACT_UV_VERSION = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+")
_VERIFY_SCRIPT = "verify_build_distributions.py"
_DIGEST_OUTPUTS = ("wheel-name", "wheel-sha256", "sdist-name", "sdist-sha256")


class MissingSetupUvError(AssertionError):
    """A workflow step list does not install uv at all."""

    def __init__(self, where: str) -> None:
        """Build the exception message."""
        super().__init__(f"{where} must set up uv")


def _action_document() -> dict[str, object]:
    """Parse the pure-Python wheel composite action.

    Returns
    -------
    dict of str to object
        The parsed action manifest.
    """
    document = load(WHEEL_ACTION.read_text(encoding="utf-8"), WHEEL_ACTION.name)
    assert isinstance(document, dict), "the wheel action must parse to a mapping"
    return typ.cast("dict[str, object]", document)


def _action_steps() -> list[dict[str, object]]:
    """Return the composite action's steps.

    Returns
    -------
    list of dict of str to object
        One narrowed mapping per declared step.
    """
    runs = _action_document().get("runs")
    assert isinstance(runs, dict), "the wheel action must declare runs"
    declared = runs.get("steps")
    assert isinstance(declared, list), "the wheel action must declare steps"
    return [typ.cast("dict[str, object]", step) for step in declared]


def _contents(permissions: object) -> str | None:
    """Return the declared ``contents`` scope, or None when absent.

    Parameters
    ----------
    permissions : object
        A parsed permissions mapping.

    Returns
    -------
    str or None
        The granted scope, when one is declared as a string.
    """
    if not isinstance(permissions, dict):
        return None
    value = permissions.get("contents")
    return value if isinstance(value, str) else None


def _checkout_steps(declared: list[dict[str, object]]) -> list[dict[str, object]]:
    """Return the checkout steps in *declared*.

    Parameters
    ----------
    declared : list of dict of str to object
        Workflow or composite action steps.

    Returns
    -------
    list of dict of str to object
        The ``actions/checkout`` entries.
    """
    return [
        step
        for step in declared
        if str(step.get("uses", "")).startswith("actions/checkout@")
    ]


def assert_checkouts_are_credential_free(
    declared: list[dict[str, object]], where: str
) -> None:
    """Require every checkout in *declared* to disable credential persistence.

    Parameters
    ----------
    declared : list of dict of str to object
        The workflow step or composite action step documents.
    where : str
        The location named in a failure.

    Notes
    -----
    A checkout must set the boolean ``false``. A quoted ``"false"`` is a
    truthy string to GitHub, so it would still persist the credential.
    """
    for index, step in enumerate(_checkout_steps(declared)):
        with_ = step.get("with")
        value = with_.get("persist-credentials") if isinstance(with_, dict) else None
        assert value is False, (
            f"{where} checkout {index} must set with.persist-credentials: false "
            f"(boolean); found {value!r}"
        )


def test_only_github_release_jobs_may_write_contents() -> None:
    """The build and attestation jobs never hold a repository write scope."""
    writers = {
        name
        for name, document in jobs(RELEASE).items()
        if _contents(typ.cast("dict[str, object]", document).get("permissions"))
        == "write"
    }
    assert writers == {"draft-release", "publish-release"}, writers


def test_the_build_job_has_read_only_contents() -> None:
    """The job that executes the build backend reads the repository only."""
    assert _contents(job(RELEASE, "build-wheels").get("permissions")) == "read", (
        "build-wheels must hold contents: read"
    )
    assert _contents(workflow_document(RELEASE).get("permissions")) != "write", (
        "the release workflow must not grant contents: write"
    )


def test_the_pypi_job_has_read_only_contents() -> None:
    """Trusted Publishing mints an OIDC token but cannot write contents."""
    permissions = job(RELEASE, "publish-pypi")["permissions"]
    assert _contents(permissions) == "read", "publish-pypi must hold contents: read"
    declared = typ.cast("dict[str, object]", permissions)
    assert declared["id-token"] == "write", (
        "publish-pypi needs id-token: write for Trusted Publishing"
    )


def test_ci_grants_read_only_contents() -> None:
    """The pull-request lane checks the build without a write scope."""
    assert workflow_document(CI).get("permissions") == {"contents": "read"}, (
        "CI must grant only contents: read"
    )


def test_every_checkout_in_release_and_ci_is_credential_free() -> None:
    """A persisted token would sit on disk beside the executable build."""
    for name in jobs(RELEASE):
        assert_checkouts_are_credential_free(steps(RELEASE, name), f"{RELEASE}:{name}")
    for name in jobs(CI):
        assert_checkouts_are_credential_free(steps(CI, name), f"{CI}:{name}")
    assert_checkouts_are_credential_free(_action_steps(), str(WHEEL_ACTION))


@pytest.mark.parametrize(
    "checkout",
    [
        {"uses": "actions/checkout@" + "0" * 40},
        {
            "uses": "actions/checkout@" + "0" * 40,
            "with": {"persist-credentials": "false"},
        },
        {
            "uses": "actions/checkout@" + "0" * 40,
            "with": {"persist-credentials": True},
        },
    ],
    ids=["omitted", "quoted-string", "true"],
)
def test_a_checkout_that_persists_credentials_is_refused(
    checkout: dict[str, object],
) -> None:
    """The contract refuses every form that leaves a token usable."""
    with pytest.raises(AssertionError):
        assert_checkouts_are_credential_free([checkout], "fixture.yml")


def test_a_credential_free_checkout_is_accepted() -> None:
    """The boolean false is the one accepted form."""
    assert_checkouts_are_credential_free(
        [
            {
                "uses": "actions/checkout@" + "0" * 40,
                "with": {"persist-credentials": False},
            }
        ],
        "fixture.yml",
    )


def test_the_build_job_calls_the_composite_action() -> None:
    """The release build runs through the reviewed composite action."""
    build_steps = steps(RELEASE, "build-wheels")
    assert_checkouts_are_credential_free(build_steps, f"{RELEASE}:build-wheels")
    composite = [
        step
        for step in build_steps
        if step.get("uses") == "./.github/actions/pure-python-wheel"
    ]
    assert len(composite) == 1, "the build job must use the wheel action once"
    assert composite[0].get("id") == "build", "its outputs need a step id"


def test_ci_build_dist_uses_the_composite_action() -> None:
    """The pull-request lane exercises the same release build action."""
    build_steps = steps(CI, "build-dist")
    assert_checkouts_are_credential_free(build_steps, f"{CI}:build-dist")
    assert any(
        step.get("uses") == "./.github/actions/pure-python-wheel"
        for step in build_steps
    ), "build-dist must use the pure-python-wheel action"


def test_the_wheel_action_calls_the_shared_release_recipe() -> None:
    """The composite action must not grow a second build path."""
    scripts = [str(step.get("run", "")) for step in _action_steps() if "run" in step]
    assert any("make release-wheel" in script for script in scripts), (
        "the wheel action must call make release-wheel"
    )
    assert not any(
        re.search(r"(?<![\w-])uv build(?![\w-])", script) for script in scripts
    ), "the release build must go through the Makefile recipe"


def _setup_uv_version(declared: list[dict[str, object]], where: str) -> str:
    """Return the exact uv release a step list installs.

    Parameters
    ----------
    declared : list of dict of str to object
        Workflow or composite action steps.
    where : str
        The location named in a failure.

    Returns
    -------
    str
        The pinned uv version.

    Raises
    ------
    MissingSetupUvError
        If the step list has no setup-uv step. The function also asserts that
        the step's version is an exact release.
    """
    for step in declared:
        if str(step.get("uses", "")).startswith("astral-sh/setup-uv@"):
            with_ = step.get("with")
            assert isinstance(with_, dict), f"{where} must pin setup-uv's version"
            version = with_.get("version")
            assert isinstance(version, str), f"{where} must pin an exact uv version"
            assert _EXACT_UV_VERSION.fullmatch(version), (
                f"{where} pins {version!r}, not an exact release"
            )
            return version
    raise MissingSetupUvError(where)


def test_the_wheel_action_and_ci_pin_the_same_uv_release() -> None:
    """Both callers of the build recipe must run the same uv frontend."""
    action_version = _setup_uv_version(_action_steps(), str(WHEEL_ACTION))
    ci_version = _setup_uv_version(steps(CI, "lint-test"), f"{CI}:lint-test")
    assert action_version == ci_version, (
        f"the wheel action pins uv {action_version}, but ci.yml pins {ci_version}"
    )


def test_the_build_job_reports_distribution_identity() -> None:
    """Publication needs the exact name and digest from the build job."""
    outputs = job(RELEASE, "build-wheels").get("outputs")
    assert isinstance(outputs, dict), "the build job must declare outputs"
    for name in _DIGEST_OUTPUTS:
        assert outputs.get(name) == "${{ steps.build.outputs." + name + " }}", (
            f"build-wheels must forward {name}"
        )


def test_the_wheel_action_reports_distribution_identity() -> None:
    """The composite action forwards the digests it measured."""
    outputs = _action_document().get("outputs")
    assert isinstance(outputs, dict), "the wheel action must declare outputs"
    for name in _DIGEST_OUTPUTS:
        entry = typ.cast("dict[str, object]", outputs.get(name))
        assert entry.get("value") == "${{ steps.digests.outputs." + name + " }}", (
            f"the wheel action must forward {name}"
        )


def test_attestation_downloads_the_build_artifact() -> None:
    """Only the build job's upload is the attested input."""
    downloads = [
        typ.cast("dict[str, object]", step.get("with"))
        for step in steps(RELEASE, "attest")
        if str(step.get("uses", "")).startswith("actions/download-artifact@")
    ]
    assert {"name": "wheels-pure", "path": "dist"} in downloads, (
        "attest must download wheels-pure into dist"
    )


def _verification_steps(job_name: str) -> list[dict[str, object]]:
    """Return the distribution-verification steps in *job_name*.

    Parameters
    ----------
    job_name : str
        A job in ``release.yml``.

    Returns
    -------
    list of dict of str to object
        The steps that run the verification helper.
    """
    return [
        step
        for step in steps(RELEASE, job_name)
        if _VERIFY_SCRIPT in str(step.get("run", ""))
    ]


def test_publication_verifies_the_build_digest() -> None:
    """Every consumer of the artefact compares it with the build report."""
    for name in ("attest", "publish-pypi", "publish-release"):
        found = _verification_steps(name)
        assert len(found) == 1, f"{name} must verify the build identity once"
        env = found[0].get("env")
        assert isinstance(env, dict), f"{name} must pass the expected values via env"
        assert env["EXPECTED_WHEEL_SHA256"] == (
            "${{ needs.build-wheels.outputs.wheel-sha256 }}"
        ), f"{name} must compare the wheel digest"
        assert env["EXPECTED_SDIST_SHA256"] == (
            "${{ needs.build-wheels.outputs.sdist-sha256 }}"
        ), f"{name} must compare the sdist digest"


def test_publication_jobs_wait_on_the_build_outputs() -> None:
    """A job cannot read another job's outputs without depending on it."""
    for name in ("attest", "publish-pypi", "publish-release"):
        needs = job(RELEASE, name).get("needs")
        match needs:
            case str():
                declared = {needs}
            case list():
                declared = {str(item) for item in needs}
            case _:
                declared = set()
        assert "build-wheels" in declared, f"{name} must need build-wheels"


@pytest.mark.parametrize(
    ("job_name", "later_step"),
    [
        ("attest", "Attest build provenance"),
        ("publish-pypi", "Publish to PyPI"),
        ("publish-release", "Upload the assets the release lacks"),
        ("publish-release", "Publish the GitHub Release"),
    ],
)
def test_verification_precedes_every_publication_action(
    job_name: str, later_step: str
) -> None:
    """A mismatch must fail before anything is attested, uploaded, or published."""
    declared = steps(RELEASE, job_name)
    names = [str(step.get("name", "")) for step in declared]
    verify_index = next(
        index
        for index, step in enumerate(declared)
        if _VERIFY_SCRIPT in str(step.get("run", ""))
    )
    assert verify_index < names.index(later_step), (
        f"{job_name} must verify before {later_step}"
    )


def test_publication_jobs_never_run_a_build_command() -> None:
    """A write-scoped job consumes the verified artefact; it never rebuilds."""
    for name in ("attest", "draft-release", "publish-pypi", "publish-release"):
        for step in steps(RELEASE, name):
            script = step.get("run")
            if isinstance(script, str):
                assert not _BUILD_COMMAND.search(script), (
                    f"{RELEASE}:{name} runs a build command: {script!r}"
                )
