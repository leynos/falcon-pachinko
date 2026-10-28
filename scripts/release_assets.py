"""Reconcile the release artefacts so PyPI and the GitHub Release agree.

A re-pushed tag rebuilds every artefact, and rebuilt wheels are rarely
byte-identical, so each filename's canonical bytes are the ones already
published: PyPI's first, then an existing GitHub Release asset, and this run's
freshly built and attested file only when neither destination has the name.
Each destination receives only the names it lacks and nothing is overwritten.

``scripts/release_assets_cli.py`` wraps this module in the command-line
interface ``release.yml`` calls, importing it wholesale, so it inherits that
wrapper's constraint: it executes inside the job that holds the PyPI
publishing token, so it resolves no third-party code (Cyclopts or falcon-pachinko)
at release time, and every network call stays in the workflow's ``curl`` and
``gh`` steps.
"""

from __future__ import annotations

import dataclasses as dc
import enum
import hashlib
import json
import os
import re
import shutil
import sys
import typing as typ
import urllib.parse
from pathlib import Path

if typ.TYPE_CHECKING:
    import collections.abc as cabc

#: The PEP 691 JSON simple index; relative file URLs resolve against it.
INDEX_URL = "https://pypi.org/simple/falcon-pachinko/"

#: Artefact names this module will stage. Wheel and sdist names never need
#: more, and the bound keeps a name safe as a path and as a TSV field.
_SAFE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]*")
_DIGEST_PREFIX = "sha256:"


class ReleaseAssetError(Exception):
    """A release asset is missing, unsafe, or holds unexpected bytes."""


class Source(enum.StrEnum):
    """Where one artefact name's canonical bytes come from."""

    PYPI = "pypi"
    GITHUB = "github"
    LOCAL = "local"


@dc.dataclass(frozen=True, slots=True)
class Plan:
    """The canonical source of each name and what each destination lacks."""

    canonical: cabc.Mapping[str, Source]
    pypi_uploads: frozenset[str]
    github_uploads: frozenset[str]


def _source_of(
    name: str, on_pypi: cabc.Collection[str], on_github: cabc.Collection[str]
) -> Source:
    """Choose PyPI, GitHub, or local bytes in that order."""
    if name in on_pypi:
        return Source.PYPI
    if name in on_github:
        return Source.GITHUB
    return Source.LOCAL


def plan(
    local: cabc.Iterable[str],
    on_pypi: cabc.Collection[str],
    on_github: cabc.Collection[str],
) -> Plan:
    """Choose each name's canonical bytes and the uploads each side needs.

    Parameters
    ----------
    local : collections.abc.Iterable[str]
        The artefact names this run built and attested.
    on_pypi : collections.abc.Collection[str]
        Names PyPI already holds.
    on_github : collections.abc.Collection[str]
        Names the tag's GitHub Release already holds, draft or published.

    Returns
    -------
    Plan
        PyPI takes precedence over GitHub, and GitHub over this run's bytes;
        each destination is sent exactly the names it lacks.

    Examples
    --------
    >>> plan(["p.whl"], set(), {"p.whl"}).canonical["p.whl"]
    <Source.GITHUB: 'github'>

    """
    names = frozenset(local)
    canonical = {name: _source_of(name, on_pypi, on_github) for name in names}
    return Plan(
        canonical=canonical,
        pypi_uploads=frozenset(name for name in names if name not in on_pypi),
        github_uploads=frozenset(name for name in names if name not in on_github),
    )


def mismatches(
    local: cabc.Iterable[str],
    pypi_digests: cabc.Mapping[str, str],
    github_digests: cabc.Mapping[str, str | None],
) -> list[str]:
    """Describe every name the two destinations do not hold identically.

    Parameters
    ----------
    local : collections.abc.Iterable[str]
        The artefact names the release must carry.
    pypi_digests : collections.abc.Mapping[str, str]
        PyPI's SHA-256 hex digest for each name it holds.
    github_digests : collections.abc.Mapping[str, str | None]
        The GitHub Release's SHA-256 hex digest per asset, ``None`` when
        GitHub reports none.

    Returns
    -------
    list[str]
        One message per missing or differing name; empty when they agree.

    Examples
    --------
    >>> mismatches(["p.whl"], {"p.whl": "ab"}, {"p.whl": "ab"})
    []

    """
    return [
        problem
        for name in sorted(set(local))
        if (problem := _mismatch(name, pypi_digests, github_digests))
    ]


def _mismatch(
    name: str,
    pypi_digests: cabc.Mapping[str, str],
    github_digests: cabc.Mapping[str, str | None],
) -> str | None:
    """Describe how one name differs between the destinations, if it does."""
    pypi, github = pypi_digests.get(name), github_digests.get(name)
    if pypi is None:
        return f"{name} is missing from PyPI"
    if github is None:
        return f"{name} is missing from the GitHub Release or has no digest"
    if pypi != github:
        return f"{name} differs: PyPI sha256 {pypi}, GitHub sha256 {github}"
    return None


@dc.dataclass(frozen=True, slots=True)
class PypiFile:
    """One file the PyPI index lists."""

    sha256: str
    url: str


def read_pypi_index(path: Path) -> dict[str, PypiFile]:
    """Map each filename in a PEP 691 JSON index to its digest and URL.

    Parameters
    ----------
    path : pathlib.Path
        The saved JSON document returned by PyPI's Simple API.

    Returns
    -------
    dict[str, PypiFile]
        Each distribution filename mapped to its SHA-256 digest and download URL.

    Raises
    ------
    ReleaseAssetError
        If the index contains malformed file entries.

    Notes
    -----
    File access and JSON decoding errors propagate.
    """
    document = json.loads(path.read_text(encoding="utf-8"))
    try:
        return {
            entry["filename"]: PypiFile(
                sha256=entry["hashes"]["sha256"].lower(),
                url=urllib.parse.urljoin(INDEX_URL, entry["url"]),
            )
            for entry in document.get("files", [])
        }
    except (KeyError, TypeError, AttributeError, ValueError) as error:
        msg = f"{path.name} has a malformed file entry: {error!r}"
        raise ReleaseAssetError(msg) from error


def read_github_assets(path: Path) -> dict[str, str | None]:
    """Map each asset in ``gh release view --json assets`` to its digest.

    Parameters
    ----------
    path : pathlib.Path
        The saved JSON response from ``gh release view --json assets``.

    Returns
    -------
    dict[str, str | None]
        Asset filenames mapped to lowercase SHA-256 hex digests, or ``None``
        when GitHub did not report a SHA-256 digest.

    Raises
    ------
    ReleaseAssetError
        If the release asset listing contains malformed entries.

    Notes
    -----
    File access and JSON decoding errors propagate.
    """
    document = json.loads(path.read_text(encoding="utf-8"))
    try:
        return {
            asset["name"]: _digest_hex(asset.get("digest"))
            for asset in document.get("assets", [])
        }
    except (KeyError, TypeError, AttributeError, ValueError) as error:
        msg = f"{path.name} has a malformed asset entry: {error!r}"
        raise ReleaseAssetError(msg) from error


def _digest_hex(digest: object) -> str | None:
    """Return the hex of a ``sha256:`` digest; ``None`` for any other form."""
    if isinstance(digest, str) and digest.startswith(_DIGEST_PREFIX):
        return digest.removeprefix(_DIGEST_PREFIX).lower()
    return None


def sha256_of(path: Path) -> str:
    """Return the SHA-256 hex digest of one file's bytes.

    Parameters
    ----------
    path : pathlib.Path
        The file whose bytes to hash.

    Returns
    -------
    str
        The lowercase SHA-256 digest in hexadecimal form.

    Notes
    -----
    File access errors propagate to the caller.
    """
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def local_names(directory: Path) -> list[str]:
    """List the artefact names in ``directory``, refusing any unsafe name.

    Parameters
    ----------
    directory : pathlib.Path
        The directory containing distribution files.

    Returns
    -------
    list[str]
        The sorted filenames found in the directory.

    Raises
    ------
    ReleaseAssetError
        If a filename is unsafe to use as an artefact path or TSV field.

    Notes
    -----
    Directory access errors propagate to the caller.
    """
    names = sorted(path.name for path in directory.iterdir())
    unsafe = [name for name in names if not _SAFE_NAME.fullmatch(name)]
    if unsafe:
        msg = f"refusing unsafe artefact names: {unsafe}"
        raise ReleaseAssetError(msg)
    return names


def _check_digest(path: Path, expected: str | None) -> None:
    """Raise unless ``path`` holds bytes with the ``expected`` digest."""
    actual = sha256_of(path)
    if expected is None or actual != expected:
        msg = f"{path.name} has sha256 {actual}, expected {expected}"
        raise ReleaseAssetError(msg)


def write_output(name: str, *, value: bool) -> None:
    """Append one ``true``/``false`` step output for the workflow.

    Parameters
    ----------
    name : str
        The GitHub Actions output name.
    value : bool
        The boolean value to write in lowercase.

    Raises
    ------
    ReleaseAssetError
        If ``GITHUB_OUTPUT`` is not set.

    Notes
    -----
    Output file access errors propagate to the caller.
    """
    output_path = os.environ.get("GITHUB_OUTPUT")
    if output_path is None:
        msg = "GITHUB_OUTPUT is required to write step outputs"
        raise ReleaseAssetError(msg)
    with Path(output_path).open("a", encoding="utf-8") as output:
        output.write(f"{name}={str(value).lower()}\n")


@dc.dataclass(frozen=True, slots=True)
class State:
    """The run's artefacts and what each destination already holds."""

    publish: Path
    pypi: dict[str, PypiFile]
    github: dict[str, str | None]

    @classmethod
    def load(cls, publish: Path, index: Path | None, assets: Path | None) -> State:
        """Read the staged artefacts and whichever destination listings exist.

        ``assets`` is optional because one command reads the PyPI index alone;
        a caller that needs the release's holdings passes the listing.

        Parameters
        ----------
        publish : pathlib.Path
            Directory containing the staged distribution files.
        index : pathlib.Path | None
            Saved PyPI Simple API response, or ``None`` when PyPI state is not
            needed.
        assets : pathlib.Path | None
            Saved GitHub Release asset listing, or ``None`` when no listing is
            available.

        Returns
        -------
        State
            The staged files and available destination listings.
        """
        pypi = read_pypi_index(index) if index is not None else {}
        return cls(publish, pypi, read_github_assets(assets) if assets else {})

    def plan(self) -> Plan:
        """Plan this run's uploads against both destinations.

        Returns
        -------
        Plan
            Canonical sources and the filenames each destination lacks.

        Notes
        -----
        Errors from listing the staged directory or validating its filenames
        propagate to the caller.
        """
        return plan(local_names(self.publish), self.pypi, self.github)


def carryover(state: State) -> list[str]:
    """Return the names whose GitHub bytes PyPI may need.

    Parameters
    ----------
    state : State
        The staged artefacts and destination listings.

    Returns
    -------
    list[str]
        Sorted filenames present in the build and the GitHub Release.

    Notes
    -----
    Errors from listing the staged directory or validating its filenames
    propagate to the caller.
    """
    return sorted(set(local_names(state.publish)) & set(state.github))


def stage_pypi(state: State, github_files: Path) -> bool:
    """Leave in ``publish`` exactly the canonical bytes PyPI lacks.

    Parameters
    ----------
    state : State
        The staged artefacts and both destinations' holdings.
    github_files : Path
        The GitHub Release assets ``draft-release`` downloaded.

    Returns
    -------
    bool
        Whether any artefact remains to upload.

    """
    decided = state.plan()
    for name, source in sorted(decided.canonical.items()):
        artefact = state.publish / name
        match source:
            case Source.PYPI:
                print(f"::notice::Skipping {name}: already on PyPI")
                artefact.unlink()
            case Source.GITHUB:
                carried = github_files / name
                _check_digest(carried, state.github[name])
                print(f"::notice::Uploading the GitHub Release's bytes for {name}")
                shutil.copyfile(carried, artefact)
            case Source.LOCAL:
                pass
    if not decided.pypi_uploads:
        print("Every artefact is already on PyPI; nothing to publish.")
    return bool(decided.pypi_uploads)


def _pypi_fetch_list(
    lacking: cabc.Iterable[str], pypi: cabc.Mapping[str, PypiFile]
) -> list[tuple[str, str]]:
    """Return the PyPI (name, url) pairs to fetch, insisting on HTTPS."""
    fetch = [(name, pypi[name].url) for name in lacking if name in pypi]
    if any(not url.startswith("https://") for _, url in fetch):
        msg = "every PyPI download must use HTTPS"
        raise ReleaseAssetError(msg)
    return fetch


def _copy_pypi_gap(
    lacking: cabc.Iterable[str],
    pypi: cabc.Mapping[str, PypiFile],
    publish: Path,
    output: Path,
) -> None:
    """Copy each lacking artefact that PyPI does not list into ``output``."""
    for name in lacking:
        if name not in pypi:
            shutil.copyfile(publish / name, output / name)


def canonical_digest(state: State, name: str) -> tuple[Source, str | None]:
    """Return which source's bytes are canonical for ``name``, and their digest.

    The digest is ``None`` only for a GitHub asset whose digest GitHub did not
    report; this run's own bytes are always hashable.

    Parameters
    ----------
    state : State
        The staged artefacts and both destinations' listings.
    name : str
        Distribution filename whose canonical source is being resolved.

    Returns
    -------
    tuple[Source, str | None]
        The canonical source and its reported digest.
    """
    source = state.plan().canonical[name]
    match source:
        case Source.PYPI:
            return source, state.pypi[name].sha256
        case Source.GITHUB:
            return source, state.github[name]
        case Source.LOCAL:
            return source, sha256_of(state.publish / name)


def attests_canonical_bytes(state: State) -> bool:
    """Judge whether this run's bundle describes the bytes the release ships.

    A bundle attests the artefacts one run built. Reconciliation can instead
    publish bytes an earlier run built — PyPI's, or an existing release
    asset's — for some or all names, because those are canonical and are never
    overwritten, while names neither destination holds keep this run's bytes.
    Attaching this run's bundle then would put a provenance claim on the release
    that part of its own assets contradict, so the bundle is withheld unless
    every name's canonical bytes are the ones this run built and attested.

    Parameters
    ----------
    state : State
        The run's artefacts and both destinations' holdings.

    Returns
    -------
    bool
        Whether every name's canonical bytes were built by this run. A name
        whose canonical source reported no digest fails the check, because the
        bundle cannot be shown to describe it.

    """
    for name in sorted(state.plan().canonical):
        source, expected = canonical_digest(state, name)
        if source is Source.LOCAL:
            continue
        if expected is None or sha256_of(state.publish / name) != expected:
            return False
    return True


def missing_from_index(
    expected: cabc.Iterable[str], listed: cabc.Collection[str]
) -> list[str]:
    """Return the names ``expected`` that one index response does not list.

    Parameters
    ----------
    expected : collections.abc.Iterable[str]
        The names the caller needs to see in the index.
    listed : collections.abc.Collection[str]
        The filenames the index response carries.

    Returns
    -------
    list[str]
        The expected names still absent, sorted; empty once the index agrees.

    Examples
    --------
    >>> missing_from_index(["a.whl", "b.whl"], {"a.whl"})
    ['b.whl']

    """
    return sorted(set(expected) - set(listed))


def _copy_unattached_bundles(bundles: Path, state: State, output: Path) -> None:
    """Copy the run's bundle into ``output`` when it attests what will ship.

    Standard error is deliberate: standard output is the caller's TSV protocol.
    """
    unattached = sorted(
        bundle for bundle in bundles.iterdir() if bundle.name not in state.github
    )
    if not unattached:
        return
    if not attests_canonical_bytes(state):
        print(
            "::notice::Not attaching this run's Sigstore bundle: the release "
            "ships bytes another run built, so the bundle would attest a "
            "different build. It remains in the attestation store.",
            file=sys.stderr,
        )
        return
    for bundle in unattached:
        shutil.copyfile(bundle, output / bundle.name)


def stage_github(state: State, bundles: Path, output: Path) -> list[tuple[str, str]]:
    """Stage what the GitHub Release lacks and list PyPI files to fetch.

    Parameters
    ----------
    state : State
        The staged distributions and existing destination assets.
    bundles : pathlib.Path
        The directory containing provenance bundles from this build.
    output : pathlib.Path
        The directory in which to stage missing release assets.

    Returns
    -------
    list[tuple[str, str]]
        Filename and HTTPS URL pairs for canonical PyPI files missing from
        the GitHub Release.

    Notes
    -----
    Errors from validating the state, reading inputs, checking URLs, or staging
    files propagate to the caller.
    """
    lacking = sorted(state.plan().github_uploads)
    fetch = _pypi_fetch_list(lacking, state.pypi)
    output.mkdir(parents=True, exist_ok=True)
    _copy_pypi_gap(lacking, state.pypi, state.publish, output)
    _copy_unattached_bundles(bundles, state, output)
    return fetch


def check_fetched(state: State, output: Path) -> None:
    """Raise unless every file fetched from PyPI matches PyPI's digest.

    Parameters
    ----------
    state : State
        The canonical sources and PyPI digests for release distributions.
    output : pathlib.Path
        The directory containing files fetched from PyPI.

    Notes
    -----
    File access errors and digest mismatches propagate to the caller.
    """
    for name in sorted(state.plan().github_uploads & set(state.pypi)):
        _check_digest(output / name, state.pypi[name].sha256)


def verify(state: State, bundles: Path) -> list[str]:
    """Describe every artefact or bundle the two destinations disagree on.

    A bundle is required only when it attests the bytes the release ships; see
    :func:`attests_canonical_bytes`. A run that reconciles any name onto other
    bytes cannot truthfully attach its own, and `verify` must not demand it.

    Parameters
    ----------
    state : State
        The staged distributions and PyPI and GitHub asset listings.
    bundles : pathlib.Path
        Directory containing the provenance bundles attached to this build.

    Returns
    -------
    list[str]
        Descriptions of missing or inconsistent destination assets.
    """
    names = local_names(state.publish)
    if not names:
        return ["No distributions were provided for verification."]
    pypi = {name: file.sha256 for name, file in state.pypi.items()}
    problems = mismatches(names, pypi, state.github)
    if not attests_canonical_bytes(state):
        return problems
    missing = sorted(
        bundle.name for bundle in bundles.iterdir() if bundle.name not in state.github
    )
    absent = (f"{name} is missing from the GitHub Release" for name in missing)
    return [*problems, *absent]
