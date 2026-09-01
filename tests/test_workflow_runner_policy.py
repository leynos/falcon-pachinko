"""Verify the repository's GitHub Actions runner-selection contract."""

from __future__ import annotations

import typing as typ
from pathlib import Path

import pytest
import yaml

WORKFLOW_DIRECTORY = Path(__file__).parents[1] / ".github" / "workflows"
NAMESPACE_LINUX_JOBS = {
    "ci.yml": {"lint-test"},
    "coverage-main.yml": {"coverage-upload"},
    "delayed-pr-comment.yml": {"delay_and_comment"},
    "get-codescene-sha.yml": {"refresh-sha"},
    "release.yml": {"pure-wheel", "release"},
}
NAMESPACE_PROFILE = "namespace-profile-default"
WHEEL_MATRIX = [
    {"os": "ubuntu-latest", "arch": "x86_64", "cibw_arch": "x86_64"},
    {"os": "ubuntu-latest", "arch": "aarch64", "cibw_arch": "aarch64"},
    {"os": "windows-latest", "arch": "x86_64", "cibw_arch": "AMD64"},
    {"os": "windows-latest", "arch": "aarch64", "cibw_arch": "ARM64"},
    {"os": "macos-latest", "arch": "x86_64", "cibw_arch": "x86_64"},
    {"os": "macos-latest", "arch": "aarch64", "cibw_arch": "arm64"},
]


def load_workflow(name: str) -> dict[str, object]:
    """Load one workflow as a mapping for runner-policy assertions."""
    contents = (WORKFLOW_DIRECTORY / name).read_text(encoding="utf-8")
    return require_mapping(yaml.safe_load(contents))


def require_mapping(value: object) -> dict[str, object]:
    """Assert that one YAML node is a string-keyed mapping."""
    assert isinstance(value, dict), "Expected a workflow mapping"
    return typ.cast("dict[str, object]", value)


@pytest.mark.parametrize("workflow_name", sorted(NAMESPACE_LINUX_JOBS))
def test_repository_owned_linux_jobs_use_namespace_profile(workflow_name: str) -> None:
    """Keep every migrated repository-owned Linux job on Namespace."""
    workflow = load_workflow(workflow_name)
    jobs = require_mapping(workflow["jobs"])

    for job_name in NAMESPACE_LINUX_JOBS[workflow_name]:
        job = require_mapping(jobs[job_name])
        assert job["runs-on"] == NAMESPACE_PROFILE, (
            f"{workflow_name}:{job_name} must use {NAMESPACE_PROFILE}"
        )


def test_native_wheel_workflow_retains_caller_selected_runners() -> None:
    """Keep native wheel builds on their documented GitHub-hosted matrix."""
    workflow = load_workflow("build-wheels.yml")
    jobs = require_mapping(workflow["jobs"])

    build_job = require_mapping(jobs["build"])
    assert build_job["runs-on"] == "${{ matrix.os }}", (
        "Native wheel builds must use the matrix-selected runner"
    )
    strategy = require_mapping(build_job["strategy"])
    matrix = require_mapping(strategy["matrix"])
    assert matrix["include"] == WHEEL_MATRIX, "Native wheel matrix changed"
