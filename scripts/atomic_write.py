"""Replace files atomically through a temporary sibling.

A small persistence primitive used by the duplication allowlist writer. It
keeps temporary-file replacement, permission preservation, and syncing in
one focused implementation.

Adapted from ``leynos/episodic`` PR #276 at commit
``d9e5ac0d254f375e2986f52d91a3b88c117c833b``.
"""

import collections.abc as cabc
import contextlib
import dataclasses as dc
import os
import pathlib
import tempfile


@dc.dataclass(frozen=True, slots=True)
class AtomicWriteOptions:
    """Configure atomic replacement behaviour."""

    create_parents: bool = True
    preserve_mode: bool = False
    sync_file: bool = False


# Read as the default for `atomic_write` rather than constructed at each call
# site, which would evaluate the call in the signature and share one mutable
# default across every caller.
_DEFAULT_OPTIONS = AtomicWriteOptions()


@contextlib.contextmanager
def _open_directory(directory: pathlib.Path) -> cabc.Iterator[int]:
    """Open a directory descriptor for a scoped operation.

    Reading a directory needs O_RDONLY, which is not portable to Python's
    buffered ``open``; the raw descriptor is yielded so the caller's work runs
    between the open and the guaranteed close. A failure in ``os.open`` itself
    leaves no descriptor to release.

    Yields
    ------
    int
        The open directory descriptor.
    """
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        yield descriptor
    finally:
        os.close(descriptor)


def atomic_write(
    path: pathlib.Path,
    content: bytes,
    *,
    options: AtomicWriteOptions = _DEFAULT_OPTIONS,
) -> None:
    """Atomically replace a path after writing a temporary sibling.

    Parameters
    ----------
    path : pathlib.Path
        Destination to replace.
    content : bytes
        Complete replacement contents.
    options : AtomicWriteOptions
        Directory creation, mode preservation, and fsync policy. The default
        creates missing parents, ignores the destination mode, and does not
        fsync.

    Raises
    ------
    RuntimeError
        If the temporary sibling file was not created before replacement.
    """
    if options.create_parents:
        path.parent.mkdir(parents=True, exist_ok=True)
    mode = path.stat().st_mode if options.preserve_mode and path.exists() else None
    temporary: pathlib.Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            delete=False, dir=path.parent, prefix=f".{path.name}."
        ) as stream:
            temporary = pathlib.Path(stream.name)
            stream.write(content)
            stream.flush()
            if options.sync_file:
                os.fsync(stream.fileno())
        if temporary is None:
            msg = "temporary sibling file was not created"
            raise RuntimeError(msg)
        if mode is not None:
            temporary.chmod(mode)
        temporary.replace(path)
        if options.sync_file:
            with _open_directory(path.parent) as descriptor:
                os.fsync(descriptor)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
