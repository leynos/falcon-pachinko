"""Tests for the checksum-pinned nose release installer and Make target."""

import hashlib
import io
import json
import os
import re
import shutil
import subprocess  # ruff: ignore[suspicious-subprocess-import] - test exercises the repository Make target
import sys
import tarfile
from collections import abc as cabc
from pathlib import Path

import install_nose as installer
import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
MAKEFILE = REPOSITORY_ROOT / "Makefile"
NOSE_VERSION, _DIGESTS = installer.load_pins()
LINUX_X86_64 = "x86_64-unknown-linux-gnu"


def _write_executable(path: Path, version: str) -> Path:
    """Create a tiny version-reporting stand-in for the nose executable."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"#!{sys.executable}\nimport sys\nprint('nose {version}')\n",
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path


def _release_archive(version: str = NOSE_VERSION) -> bytes:
    """Create a local tar.xz release with one executable named ``nose``."""
    payload = (f"#!{sys.executable}\nimport sys\nprint('nose {version}')\n").encode()
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode="w:xz") as release:
        member = tarfile.TarInfo("nose")
        member.size = len(payload)
        member.mode = 0o755
        release.addfile(member, io.BytesIO(payload))
    return archive.getvalue()


def _write_pins(directory: Path, *, version: str, archive: bytes) -> tuple[Path, Path]:
    """Write a minimal pyproject and digest manifest for installer tests."""
    pyproject = directory / "pyproject.toml"
    pyproject.write_text(f'[tool.nose]\nversion = "{version}"\n', encoding="utf-8")
    manifest = directory / "nose-release-digests.json"
    manifest.write_text(
        json.dumps({
            "version": version,
            "sha256": {
                LINUX_X86_64: hashlib.sha256(archive).hexdigest(),
            },
        }),
        encoding="utf-8",
    )
    return pyproject, manifest


class TestReleaseSelection:
    """Only supported platforms map to versioned upstream release assets."""

    @pytest.mark.parametrize(
        ("system", "machine", "expected"),
        [
            pytest.param("Linux", "x86_64", LINUX_X86_64, id="linux-x86-64"),
            pytest.param(
                "Linux", "aarch64", "aarch64-unknown-linux-gnu", id="linux-arm64"
            ),
            pytest.param("Darwin", "x86_64", "x86_64-apple-darwin", id="macos-x86"),
            pytest.param("Darwin", "arm64", "aarch64-apple-darwin", id="macos-arm64"),
        ],
    )
    def test_maps_supported_platforms(
        self, system: str, machine: str, expected: str
    ) -> None:
        """The target determines the archive and its platform-specific digest."""
        target = installer.platform_target(
            system=system,
            machine=machine,
            libc_name="glibc" if system == "Linux" else None,
        )
        assert target == expected, "Supported platform must map to its target."
        assert installer.asset_filename(target) == f"nose-cli-{target}.tar.xz", (
            "The release archive name must match the selected platform."
        )

    def test_rejects_platforms_without_an_approved_binary(self) -> None:
        """Unsupported operating systems fail without compiling or guessing."""
        with pytest.raises(installer.NoseInstallError, match="no checksum-approved"):
            installer.platform_target(system="Windows", machine="AMD64")

    @pytest.mark.parametrize("libc_name", ["musl", ""])
    def test_rejects_linux_without_a_known_glibc_release_target(
        self, libc_name: str
    ) -> None:
        """Linux releases cannot be selected for musl or unknown libc hosts."""
        with pytest.raises(installer.NoseInstallError, match="glibc only"):
            installer.platform_target(
                system="Linux", machine="x86_64", libc_name=libc_name
            )


class TestVerifiedArchive:
    """Archive integrity and safe extraction boundaries."""

    def test_verifies_checksum_and_reads_only_the_executable(self) -> None:
        """The expected digest is checked before the executable is returned."""
        archive = _release_archive()
        payload = installer.verified_binary(
            archive, expected_sha256=hashlib.sha256(archive).hexdigest()
        )
        assert payload.startswith(f"#!{sys.executable}".encode()), (
            "Verified archive extraction must return the Python stub executable."
        )

    def test_rejects_checksum_mismatch(self) -> None:
        """An unapproved binary payload is never installed."""
        with pytest.raises(installer.NoseInstallError, match="SHA-256 mismatch"):
            installer.verified_binary(_release_archive(), expected_sha256="0" * 64)

    def test_rejects_archives_without_one_nose_executable(self) -> None:
        """An archive cannot select an arbitrary file or path for installation."""
        archive_stream = io.BytesIO()
        with tarfile.open(fileobj=archive_stream, mode="w:xz") as release:
            payload = b"not nose"
            member = tarfile.TarInfo("other-binary")
            member.size = len(payload)
            release.addfile(member, io.BytesIO(payload))
        archive = archive_stream.getvalue()
        with pytest.raises(installer.NoseInstallError, match="exactly one nose"):
            installer.verified_binary(
                archive, expected_sha256=hashlib.sha256(archive).hexdigest()
            )


class TestEnsureInstalled:
    """Cached-binary, download, checksum, and post-install behaviour."""

    def test_matching_relative_override_is_a_noop(self, tmp_path: Path) -> None:
        """A correct override resolves from the repository root and skips download."""
        archive = _release_archive()
        pyproject, manifest = _write_pins(
            tmp_path, version=NOSE_VERSION, archive=archive
        )
        binary = _write_executable(tmp_path / "custom" / "nose", NOSE_VERSION)
        messages: list[str] = []

        def unexpected_download(_url: str) -> bytes:
            pytest.fail("A matching cached binary must not trigger a download")

        process_calls: list[tuple[list[str], Path, dict[str, str]]] = []

        def process_runner(
            command: cabc.Sequence[str],
            repository_root: Path,
            environment: cabc.Mapping[str, str],
        ) -> subprocess.CompletedProcess[str]:
            process_calls.append((list(command), repository_root, dict(environment)))
            return installer._run_process(command, repository_root, environment)

        result = installer.ensure_installed(
            installer.InstallerContext(
                repository_root=tmp_path,
                pyproject_path=pyproject,
                manifest_path=manifest,
                environment={"NOSE_BIN": "custom/nose"},
                downloader=unexpected_download,
                output=messages.append,
                process_runner=process_runner,
            )
        )
        assert result == binary.resolve(), "A matching override must be reused."
        assert "already installed" in messages[0], (
            "Reusing the override must report the no-op installation."
        )
        assert process_calls == [
            (
                [str(binary.resolve()), "--version"],
                tmp_path,
                {"NOSE_BIN": "custom/nose"},
            )
        ], "Cached-binary verification must use the injected root and environment."

    def test_installs_only_the_platform_archive_and_verifies_it(
        self, tmp_path: Path
    ) -> None:
        """A stale binary is atomically replaced from the fixed release URL."""
        archive = _release_archive()
        pyproject, manifest = _write_pins(
            tmp_path, version=NOSE_VERSION, archive=archive
        )
        target = tmp_path / "tools" / "nose"
        _write_executable(target, "0.19.0")
        downloads: list[str] = []
        messages: list[str] = []

        def download(url: str) -> bytes:
            downloads.append(url)
            return archive

        process_calls: list[tuple[list[str], Path, dict[str, str]]] = []

        def process_runner(
            command: cabc.Sequence[str],
            repository_root: Path,
            environment: cabc.Mapping[str, str],
        ) -> subprocess.CompletedProcess[str]:
            process_calls.append((list(command), repository_root, dict(environment)))
            return installer._run_process(command, repository_root, environment)

        context = installer.InstallerContext(
            repository_root=tmp_path,
            pyproject_path=pyproject,
            manifest_path=manifest,
            environment={"NOSE_BIN": "tools/nose"},
            system="Linux",
            machine="x86_64",
            libc_name="glibc",
            downloader=download,
            output=messages.append,
            process_runner=process_runner,
        )
        result = installer.ensure_installed(context)
        assert result == target.resolve(), "The installer must return its target."
        assert downloads == [
            (
                f"https://github.com/corca-ai/nose/releases/download/v{NOSE_VERSION}/"
                f"nose-cli-{LINUX_X86_64}.tar.xz"
            )
        ], "Only the checksum-approved platform archive may be downloaded."
        assert installer._run_version(result, context) == f"nose {NOSE_VERSION}", (
            "The installed executable must report the configured version."
        )
        assert len(process_calls) == 3, "Each verification must run once."
        assert all(
            call[1] == tmp_path and call[2] == {"NOSE_BIN": "tools/nose"}
            for call in process_calls
        ), "Cached and installed verification must use the injected context."
        assert result.stat().st_mode & 0o777 == 0o755, (
            "The installed executable must retain executable permissions."
        )
        assert any(message.startswith("Verified ") for message in messages), (
            "The installer must report successful archive verification."
        )

    @pytest.mark.parametrize(
        "url",
        [
            "file:///etc/passwd",
            "http://github.com/corca-ai/nose/releases/download/v0.20.0/nose.tar.xz",
            "https://github.com.attacker.invalid/corca-ai/nose/releases/download/v0.20.0/nose.tar.xz",
            "https://attacker@github.com/corca-ai/nose/releases/download/v0.20.0/nose.tar.xz",
            "https://github.com:bad/corca-ai/nose/releases/download/v0.20.0/nose.tar.xz",
        ],
    )
    def test_rejects_urls_outside_the_trusted_release_host(self, url: str) -> None:
        """Only HTTPS release paths on the official GitHub host are accepted."""
        with pytest.raises(installer.NoseInstallError, match="outside the pinned"):
            installer.download_archive(url)

    def test_fails_if_installed_archive_reports_another_version(
        self, tmp_path: Path
    ) -> None:
        """A checksum-valid archive still must report the configured version."""
        archive = _release_archive("0.19.0")
        pyproject, manifest = _write_pins(
            tmp_path, version=NOSE_VERSION, archive=archive
        )
        with pytest.raises(
            installer.NoseInstallError,
            match=re.escape("expected 'nose 0.20.0'"),
        ):
            installer.ensure_installed(
                installer.InstallerContext(
                    repository_root=tmp_path,
                    pyproject_path=pyproject,
                    manifest_path=manifest,
                    environment={"NOSE_BIN": "tools/nose"},
                    system="Linux",
                    machine="x86_64",
                    libc_name="glibc",
                    downloader=lambda _url: archive,
                    output=lambda _message: None,
                )
            )

    def test_reports_filesystem_failure_as_install_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A failed atomic install becomes an actionable installer diagnostic."""
        archive = _release_archive()
        pyproject, manifest = _write_pins(
            tmp_path, version=NOSE_VERSION, archive=archive
        )

        def fail_write(*_args: object, **_kwargs: object) -> None:
            raise PermissionError

        monkeypatch.setattr(installer, "atomic_write", fail_write)
        with pytest.raises(
            installer.NoseInstallError, match="cannot install nose binary"
        ):
            installer.ensure_installed(
                installer.InstallerContext(
                    repository_root=tmp_path,
                    pyproject_path=pyproject,
                    manifest_path=manifest,
                    environment={"NOSE_BIN": "tools/nose"},
                    system="Linux",
                    machine="x86_64",
                    libc_name="glibc",
                    downloader=lambda _url: archive,
                    output=lambda _message: None,
                )
            )

    def test_fails_when_the_manifest_and_configuration_pins_differ(
        self, tmp_path: Path
    ) -> None:
        """The installer has no independent version pin to drift from config."""
        pyproject, manifest = _write_pins(
            tmp_path, version=NOSE_VERSION, archive=_release_archive()
        )
        manifest.write_text('{"version": "0.19.0", "sha256": {}}', encoding="utf-8")
        with pytest.raises(installer.NoseInstallError, match="does not match"):
            installer.load_pins(pyproject_path=pyproject, manifest_path=manifest)


@pytest.mark.skipif(shutil.which("make") is None, reason="make is required")
def test_make_install_nose_reuses_a_valid_cached_override(tmp_path: Path) -> None:
    """The public Make target verifies a cached override without downloading."""
    nose = _write_executable(tmp_path / "nose", NOSE_VERSION)
    result = subprocess.run(  # ruff: ignore[subprocess-without-shell-equals-true] - fixed Make target and local test binary
        [
            shutil.which("make") or "make",
            "--no-print-directory",
            "-f",
            str(MAKEFILE),
            "install-nose",
            f"NOSE_BIN={nose}",
        ],
        cwd=REPOSITORY_ROOT,
        env={**os.environ, "UV_CACHE_DIR": ".uv-cache", "UV_TOOL_DIR": ".uv-tools"},
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "already installed" in result.stdout, (
        "The Make target must reuse a matching cached nose executable."
    )
