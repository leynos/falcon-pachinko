#!/usr/bin/env python3
"""Install the pinned, checksum-verified nose release binary.

The version comes from ``[tool.nose]``. Only official v0.20.0 release
archives named in ``tools/nose-release-digests.json`` are accepted; this
installer never compiles nose or bootstraps another installer.
"""

from __future__ import annotations

import dataclasses as dc
import hashlib
import json
import os
import platform
import re
import subprocess  # ruff: ignore[suspicious-subprocess-import] - runs the verified local nose binary
import sys
import tarfile
import tomllib
import typing as typ
import urllib.error
import urllib.parse
import urllib.request
from io import BytesIO
from itertools import starmap
from pathlib import Path, PurePosixPath

if typ.TYPE_CHECKING:
    from collections import abc as cabc

from atomic_write import AtomicWriteOptions, atomic_write

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BINARY = REPOSITORY_ROOT / ".tools" / "nose" / "nose"
MANIFEST = REPOSITORY_ROOT / "tools" / "nose-release-digests.json"
PYPROJECT = REPOSITORY_ROOT / "pyproject.toml"
RELEASE_BASE = "https://github.com/corca-ai/nose/releases/download"
RELEASE_PATH_PREFIX = "/corca-ai/nose/releases/download/v"
MAX_ARCHIVE_BYTES = 100 * 1024 * 1024
INSTALL_TIMEOUT_SECONDS = 15


class NoseInstallError(RuntimeError):
    """Raised when a trusted, working pinned detector cannot be installed."""


def platform_target(*, system: str, machine: str, libc_name: str | None = None) -> str:
    """Map the host platform to an upstream prebuilt release target."""
    host_libc = platform.libc_ver()[0] if libc_name is None else libc_name
    if system == "Linux" and host_libc != "glibc":
        msg = (
            "nose has checksum-approved Linux binaries for glibc only; "
            f"detected {host_libc or 'an unknown libc'}"
        )
        raise NoseInstallError(msg)
    operating_system = {"Linux": "unknown-linux-gnu", "Darwin": "apple-darwin"}.get(
        system
    )
    architecture = {
        "x86_64": "x86_64",
        "AMD64": "x86_64",
        "aarch64": "aarch64",
        "arm64": "aarch64",
    }.get(machine)
    if operating_system is None or architecture is None:
        msg = (
            f"nose has no checksum-approved prebuilt binary for {system}/{machine}; "
            "the gate supports Linux glibc and macOS on x86-64 or AArch64"
        )
        raise NoseInstallError(msg)
    return f"{architecture}-{operating_system}"


def load_pins(
    *, pyproject_path: Path = PYPROJECT, manifest_path: Path = MANIFEST
) -> tuple[str, dict[str, str]]:
    """Read the single nose version pin and its platform-specific checksums."""
    project, manifest = _read_pin_documents(pyproject_path, manifest_path)
    version = _configured_nose_version(project)
    return version, _release_digests(manifest, version)


def _read_pin_documents(
    pyproject_path: Path, manifest_path: Path
) -> tuple[dict[str, object], object]:
    """Load the project table and checksum manifest with one diagnostic."""
    try:
        with pyproject_path.open("rb") as stream:
            project = tomllib.load(stream)
        with manifest_path.open(encoding="utf-8") as stream:
            manifest = json.load(stream)
    except (OSError, json.JSONDecodeError, tomllib.TOMLDecodeError) as error:
        msg = f"cannot read nose release pins: {error}"
        raise NoseInstallError(msg) from error
    return project, manifest


def _configured_nose_version(project: dict[str, object]) -> str:
    """Validate the repository's one authoritative nose version."""
    tool = project.get("tool")
    nose = tool.get("nose") if isinstance(tool, dict) else None
    version = nose.get("version") if isinstance(nose, dict) else None
    if not isinstance(version, str) or not re.fullmatch(r"\d+\.\d+\.\d+", version):
        msg = "pyproject.toml [tool.nose].version must be a pinned X.Y.Z release"
        raise NoseInstallError(msg)
    return version


def _release_digests(manifest: object, version: str) -> dict[str, str]:
    """Validate the checksum manifest against the configured release."""
    raw_digests = _checksum_table(manifest, version)
    return dict(starmap(_validated_digest_entry, raw_digests.items()))


def _checksum_table(manifest: object, version: str) -> dict[object, object]:
    """Check the manifest release and return its non-empty digest table."""
    if not isinstance(manifest, dict) or manifest.get("version") != version:
        msg = "nose release digest manifest version does not match [tool.nose].version"
        raise NoseInstallError(msg)
    raw_digests = manifest.get("sha256")
    if not isinstance(raw_digests, dict) or not raw_digests:
        msg = "nose release digest manifest must contain platform SHA-256 values"
        raise NoseInstallError(msg)
    return raw_digests


def _validated_digest_entry(target: object, digest: object) -> tuple[str, str]:
    """Validate one platform name and its lowercase SHA-256 digest."""
    if not isinstance(target, str) or not isinstance(digest, str):
        msg = "nose release digest manifest contains an invalid platform checksum"
        raise NoseInstallError(msg)
    if re.fullmatch(r"[0-9a-f]{64}", digest) is None:
        msg = "nose release digest manifest contains an invalid platform checksum"
        raise NoseInstallError(msg)
    return target, digest


def binary_path(
    *, repository_root: Path = REPOSITORY_ROOT, environment: cabc.Mapping[str, str]
) -> Path:
    """Resolve ``NOSE_BIN`` against the repository root when it is relative."""
    override = environment.get("NOSE_BIN")
    candidate = (
        Path(override) if override else repository_root / ".tools" / "nose" / "nose"
    )
    if not candidate.is_absolute():
        candidate = repository_root / candidate
    return candidate.resolve()


def asset_filename(target: str) -> str:
    """Return the versioned upstream archive name for one approved target."""
    return f"nose-cli-{target}.tar.xz"


def release_url(version: str, target: str) -> str:
    """Return the fixed GitHub release URL for a version and platform."""
    return f"{RELEASE_BASE}/v{version}/{asset_filename(target)}"


def download_archive(url: str) -> bytes:
    """Download one bounded release archive from the official GitHub URL."""
    if not _is_trusted_release_url(url):
        msg = "refusing a nose archive URL outside the pinned GitHub release"
        raise NoseInstallError(msg)
    try:
        with urllib.request.urlopen(  # ruff: ignore[suspicious-url-open-usage] - URL is HTTPS GitHub and the archive digest is checked
            url, timeout=60
        ) as response:
            archive = response.read(MAX_ARCHIVE_BYTES + 1)
    except (OSError, urllib.error.URLError) as error:
        msg = f"cannot download the pinned nose release from GitHub: {error}"
        raise NoseInstallError(msg) from error
    if not archive or len(archive) > MAX_ARCHIVE_BYTES:
        msg = "the pinned nose release archive is empty or exceeds the 100 MiB limit"
        raise NoseInstallError(msg)
    return archive


def _is_trusted_release_url(url: str) -> bool:
    """Report whether a URL names the HTTPS nose release path on GitHub."""
    try:
        parsed = urllib.parse.urlsplit(url)
    except ValueError:
        return False
    try:
        port = parsed.port
    except ValueError:
        return False
    is_trusted = parsed.scheme == "https"
    is_trusted &= parsed.netloc == "github.com"
    is_trusted &= parsed.username is None
    is_trusted &= parsed.password is None
    is_trusted &= port is None
    is_trusted &= parsed.path.startswith(RELEASE_PATH_PREFIX)
    is_trusted &= not parsed.query
    is_trusted &= not parsed.fragment
    return is_trusted


def verified_binary(archive: bytes, *, expected_sha256: str) -> bytes:
    """Verify an archive and return its single nose executable payload."""
    actual_sha256 = hashlib.sha256(archive).hexdigest()
    if actual_sha256 != expected_sha256:
        msg = (
            "nose release SHA-256 mismatch: "
            f"expected {expected_sha256}, got {actual_sha256}"
        )
        raise NoseInstallError(msg)
    try:
        with tarfile.open(fileobj=BytesIO(archive), mode="r:xz") as release:
            member = _executable_member(release)
            payload = _read_executable_payload(release, member)
    except (OSError, tarfile.TarError) as error:
        msg = f"cannot read the verified nose release archive: {error}"
        raise NoseInstallError(msg) from error
    if not payload or len(payload) != member.size:
        msg = "nose release executable is empty or truncated"
        raise NoseInstallError(msg)
    return payload


def _executable_member(release: tarfile.TarFile) -> tarfile.TarInfo:
    """Select the one regular, path-safe nose binary in a release archive."""
    candidates = [
        member for member in release.getmembers() if _is_nose_executable(member)
    ]
    if len(candidates) != 1:
        msg = "nose release archive must contain exactly one nose executable"
        raise NoseInstallError(msg)
    member = candidates[0]
    if member.size > MAX_ARCHIVE_BYTES:
        msg = "nose release executable exceeds the 100 MiB limit"
        raise NoseInstallError(msg)
    return member


def _is_nose_executable(member: tarfile.TarInfo) -> bool:
    """Reject links, unrelated members, and archive paths that can escape."""
    path = PurePosixPath(member.name)
    return (
        member.isfile()
        and path.name in {"nose", "nose-cli"}
        and ".." not in path.parts
        and not path.is_absolute()
    )


def _read_executable_payload(
    release: tarfile.TarFile, member: tarfile.TarInfo
) -> bytes:
    """Read a bounded regular-file payload from the verified archive."""
    stream = release.extractfile(member)
    if stream is None:
        msg = "cannot read the nose executable from its release archive"
        raise NoseInstallError(msg)
    return stream.read(MAX_ARCHIVE_BYTES + 1)


@dc.dataclass(frozen=True, slots=True)
class InstallerContext:
    """Inject filesystem, platform, download, and output boundaries for install."""

    repository_root: Path = REPOSITORY_ROOT
    pyproject_path: Path = PYPROJECT
    manifest_path: Path = MANIFEST
    environment: cabc.Mapping[str, str] | None = None
    system: str | None = None
    machine: str | None = None
    libc_name: str | None = None
    downloader: cabc.Callable[[str], bytes] = download_archive
    output: cabc.Callable[[str], None] = print


def _run_version(binary: Path) -> str:
    """Run ``--version`` on a local detector and return its standard output."""
    try:
        result = subprocess.run(  # ruff: ignore[subprocess-without-shell-equals-true] - executes only the configured local binary
            [str(binary), "--version"],
            cwd=REPOSITORY_ROOT,
            check=False,
            capture_output=True,
            text=True,
            timeout=INSTALL_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        msg = f"cannot verify {binary} --version: {error}"
        raise NoseInstallError(msg) from error
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        msg = f"{binary} --version exited with status {result.returncode}: {detail}"
        raise NoseInstallError(msg)
    return result.stdout.strip()


def _check_version(binary: Path, version: str) -> bool:
    """Report whether a local binary is the exact expected nose release."""
    try:
        return _run_version(binary) == f"nose {version}"
    except NoseInstallError:
        return False


def _sync_file_and_directory(binary: Path) -> None:
    """Persist the executable mode and directory entry after atomic replace."""
    with binary.open("rb") as stream:
        os.fsync(stream.fileno())
    descriptor = os.open(binary.parent, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def ensure_installed(context: InstallerContext | None = None) -> Path:
    """Reuse or install the pinned platform release, then verify its version."""
    settings = InstallerContext() if context is None else context
    env = os.environ if settings.environment is None else settings.environment
    version, digests = load_pins(
        pyproject_path=settings.pyproject_path,
        manifest_path=settings.manifest_path,
    )
    binary = binary_path(repository_root=settings.repository_root, environment=env)
    if binary.is_file() and _check_version(binary, version):
        settings.output(f"nose {version} already installed at {binary}")
        return binary
    executable = _download_release(settings, version, digests, binary)
    try:
        atomic_write(
            binary,
            executable,
            options=AtomicWriteOptions(
                create_parents=True,
                preserve_mode=False,
                sync_file=True,
            ),
        )
        binary.chmod(0o755)
        _sync_file_and_directory(binary)
    except OSError as error:
        msg = f"cannot install nose binary at {binary}: {error}"
        raise NoseInstallError(msg) from error
    reported = _run_version(binary)
    if reported != f"nose {version}":
        msg = f"installed detector reports {reported!r}; expected 'nose {version}'"
        raise NoseInstallError(msg)
    settings.output(f"Verified {binary}: {reported}")
    return binary


def _download_release(
    context: InstallerContext,
    version: str,
    digests: dict[str, str],
    binary: Path,
) -> bytes:
    """Download and verify the release selected for the current host."""
    target = platform_target(
        system=platform.system() if context.system is None else context.system,
        machine=platform.machine() if context.machine is None else context.machine,
        libc_name=context.libc_name,
    )
    digest = digests.get(target)
    if digest is None:
        msg = f"nose {version} has no checksum-approved release for {target}"
        raise NoseInstallError(msg)
    context.output(f"Installing nose {version} for {target} into {binary}")
    return verified_binary(
        context.downloader(release_url(version, target)), expected_sha256=digest
    )


def main() -> int:
    """Install the configured detector or print an actionable failure."""
    try:
        ensure_installed()
    except NoseInstallError as error:
        print(f"nose installation error: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
