"""Parse `gh attestation verify --format json` output for release provenance.

Match the exact asset filename and SHA-256 subject. GitHub CLI policy flags
establish repository, signer-workflow and tag identity; this parser does not
independently verify cryptographic trust. `stage_pypi()` uses this check before
carrying GitHub asset bytes.
"""

from __future__ import annotations

import json
import typing as typ

if typ.TYPE_CHECKING:
    import pathlib as pth


class ProvenanceRecordError(Exception):
    """A verifier record is absent, unreadable, malformed, or mismatching.

    Parameters
    ----------
    message : str
        Safe diagnostic that identifies the record failure category.

    Notes
    -----
    Missing records are represented by ``None`` from
    :func:`read_provenance_record`, then rejected by
    :func:`require_matching_record`.
    """


def read_provenance_record(record: pth.Path) -> object | None:
    """Read one verifier record, returning ``None`` when it is absent.

    Parameters
    ----------
    record : pathlib.Path
        Verifier result file to read.

    Returns
    -------
    object | None
        Decoded JSON from the verifier, or ``None`` when the file is absent.

    Raises
    ------
    ProvenanceRecordError
        If the file cannot be read, is not UTF-8, or contains invalid JSON.

    Examples
    --------
    >>> from pathlib import Path
    >>> read_provenance_record(Path("missing.json")) is None
    True
    """
    try:
        content = record.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as error:
        msg = "cannot read provenance record"
        raise ProvenanceRecordError(msg) from error
    except UnicodeError as error:
        msg = "provenance record is not valid UTF-8"
        raise ProvenanceRecordError(msg) from error

    try:
        return json.loads(content)
    except json.JSONDecodeError as error:
        msg = "provenance record is not valid JSON"
        raise ProvenanceRecordError(msg) from error


def has_matching_subject(document: object, name: str, digest: str) -> bool:
    """Return whether verifier JSON binds ``name`` to the expected SHA-256.

    Parameters
    ----------
    document : object
        JSON-decoded output from ``gh attestation verify --format json``.
    name : str
        Exact release asset filename to find in a statement subject.
    digest : str
        Expected SHA-256 hexadecimal digest; comparison ignores letter case.

    Returns
    -------
    bool
        Whether a non-empty verification array contains the exact filename and
        digest.

    Examples
    --------
    >>> digest = "a" * 64
    >>> record = {
    ...     "verificationResult": {
    ...         "statement": {
    ...             "subject": [{"name": "package.whl", "digest": {"sha256": digest}}]
    ...         }
    ...     }
    ... }
    >>> has_matching_subject([record], "package.whl", digest)
    True
    """
    match document:
        case list() as results:
            return any(_result_matches(result, name, digest) for result in results)
        case _:
            return False


def require_matching_record(record_path: pth.Path, name: str, digest: str) -> None:
    """Require one verifier record to bind an asset name to its SHA-256.

    Parameters
    ----------
    record_path : pathlib.Path
        JSON file returned by ``gh attestation verify --format json``.
    name : str
        Exact release asset filename to find in the record's subject.
    digest : str
        Expected SHA-256 hexadecimal digest; comparison ignores letter case.

    Raises
    ------
    ProvenanceRecordError
        If the record is absent, unreadable, malformed, or does not match.
    """
    record = read_provenance_record(record_path)
    if record is None:
        msg = "no verified build provenance record"
        raise ProvenanceRecordError(msg)
    if not has_matching_subject(record, name, digest):
        msg = "no matching verified build provenance"
        raise ProvenanceRecordError(msg)


def _result_matches(result: object, name: str, digest: str) -> bool:
    """Check one verifier envelope for a matching statement."""
    match result:
        case {"verificationResult": {"statement": statement} as verification} if (
            isinstance(result, dict) and isinstance(verification, dict)
        ):
            return _statement_matches(statement, name, digest)
        case _:
            return False


def _statement_matches(statement: object, name: str, digest: str) -> bool:
    """Check the JSON statement's subject list without accepting other mappings."""
    match statement:
        case {"subject": list() as subjects} if isinstance(statement, dict):
            return any(_subject_matches(subject, name, digest) for subject in subjects)
        case _:
            return False


def _subject_matches(subject: object, name: str, digest: str) -> bool:
    """Check one statement subject's exact filename and SHA-256."""
    match subject:
        case {
            "name": subject_name,
            "digest": {"sha256": str() as sha256} as subject_digest,
        } if isinstance(subject, dict) and isinstance(subject_digest, dict):
            return subject_name == name and sha256.lower() == digest.lower()
        case _:
            return False
