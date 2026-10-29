"""Validate verifier records before trusting carried release assets."""

from __future__ import annotations

import json
import typing as typ

import pytest

from scripts.release_provenance import (
    ProvenanceRecordError,
    has_matching_subject,
    require_matching_record,
)

if typ.TYPE_CHECKING:
    from pathlib import Path


def _record(
    name: str = "package.whl", digest: str = "a" * 64
) -> list[dict[str, object]]:
    """Return the verifier's structured subject result."""
    return [
        {
            "verificationResult": {
                "statement": {"subject": [{"name": name, "digest": {"sha256": digest}}]}
            }
        }
    ]


@pytest.mark.parametrize(
    "document",
    [
        None,
        {},
        [],
        [None],
        [{}],
        [{"verificationResult": {"statement": None}}],
        [{"verificationResult": {"statement": {"subject": None}}}],
        [{"verificationResult": {"statement": {"subject": [None]}}}],
        [
            {
                "verificationResult": {
                    "statement": {"subject": [{"name": "package.whl", "digest": None}]}
                }
            }
        ],
        [
            {
                "verificationResult": {
                    "statement": {
                        "subject": [{"name": "package.whl", "digest": {"sha256": None}}]
                    }
                }
            }
        ],
    ],
)
def test_unstructured_verification_cannot_authorize_a_carry(document: object) -> None:
    """Every JSON layer must have the verifier's expected structure."""
    assert not has_matching_subject(document, "package.whl", "a" * 64), (
        "malformed verification records cannot authorize carried bytes"
    )


def test_verified_subject_matches_exact_name_and_digest() -> None:
    """Equivalent digest spelling does not relax exact filename matching."""
    assert has_matching_subject(_record(digest="A" * 64), "package.whl", "a" * 64), (
        "accept matching hexadecimal digests regardless of case"
    )
    assert not has_matching_subject(_record(), "other.whl", "a" * 64), (
        "a different filename cannot gain authority from matching bytes"
    )
    assert not has_matching_subject(_record(), "package.whl", "b" * 64), (
        "a different digest cannot gain authority from the filename"
    )


@pytest.mark.parametrize("content", [None, "not JSON", "[]", "{}"])
def test_absent_or_invalid_record_is_rejected(
    tmp_path: Path, content: str | None
) -> None:
    """A successful verifier record is required before staging untrusted bytes."""
    record = tmp_path / "record.json"
    if content is not None:
        record.write_text(content, encoding="utf-8")
    with pytest.raises(ProvenanceRecordError, match="provenance"):
        require_matching_record(record, "package.whl", "a" * 64)


def test_matching_record_is_accepted(tmp_path: Path) -> None:
    """A valid verifier record authorizes its exact named SHA-256 subject."""
    record = tmp_path / "record.json"
    record.write_text(json.dumps(_record()), encoding="utf-8")
    require_matching_record(record, "package.whl", "a" * 64)


@pytest.mark.parametrize("representation", ["directory", "non-utf8"])
def test_unreadable_record_is_contextual(tmp_path: Path, representation: str) -> None:
    """Invalid filesystem inputs remain explicit provenance failures."""
    record = tmp_path / "record"
    match representation:
        case "directory":
            record.mkdir()
        case _:
            record.write_bytes(b"\xff")
    with pytest.raises(ProvenanceRecordError, match="provenance record"):
        require_matching_record(record, "package.whl", "a" * 64)
