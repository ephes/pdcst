"""Sibling temporary files with ordinary creation and replacement permissions."""

import errno
import os
import stat
import uuid
from contextlib import contextmanager


class PublicationDurabilityError(OSError):
    """Replacement is visible, but its directory entry could not be synced."""

    def __init__(self, path):
        self.path = path
        super().__init__(
            f"Published {path}; durability uncertain (directory sync failed)"
        )


def sync_directory(path, *, directory_fd=None):
    """Persist preceding name changes before reporting durable publication."""
    descriptor = (
        os.dup(directory_fd)
        if directory_fd is not None
        else os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    )
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


@contextmanager
def replacement_file(target, *, binary=False, directory_fd=None):
    # Short name: independent of the length of the destination name.
    temporary = target.with_name(f".{uuid.uuid4().hex}.tmp")

    def local(path):
        return path.name if directory_fd is not None else path

    descriptor = os.open(
        local(temporary),
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
        0o666,
        **({"dir_fd": directory_fd} if directory_fd is not None else {}),
    )
    try:
        # Keep descriptor ownership here even if stream construction fails midway.
        try:
            # Capture umask-filtered new-file permissions while the file is empty;
            # never expose payload through the staging name while writing it.
            creation_mode = stat.S_IMODE(os.fstat(descriptor).st_mode)
            os.fchmod(descriptor, 0o600)
            with os.fdopen(
                descriptor,
                "wb" if binary else "w",
                closefd=False,
                **({} if binary else {"encoding": "utf-8"}),
            ) as output:
                yield output
                output.flush()
                try:
                    mode = stat.S_IMODE(
                        target.stat().st_mode
                        if directory_fd is None
                        else os.stat(
                            target.name, dir_fd=directory_fd, follow_symlinks=False
                        ).st_mode
                    )
                except FileNotFoundError:
                    mode = creation_mode
                os.fchmod(output.fileno(), mode)
                os.fsync(output.fileno())
        finally:
            os.close(descriptor)
        if directory_fd is None:
            os.replace(temporary, target)
        else:
            os.replace(
                local(temporary),
                local(target),
                src_dir_fd=directory_fd,
                dst_dir_fd=directory_fd,
            )
        try:
            if directory_fd is None:
                sync_directory(target.parent)
            else:
                sync_directory(target.parent, directory_fd=directory_fd)
        except (OSError, KeyboardInterrupt):
            raise PublicationDurabilityError(target) from None
    finally:
        try:
            if directory_fd is None:
                temporary.unlink()
            else:
                os.unlink(temporary.name, dir_fd=directory_fd)
        except FileNotFoundError:
            pass


class DirectoryAdmissionError(ValueError):
    """A directory component cannot be admitted or has been replaced."""


class DirectoryIdentityChanged(DirectoryAdmissionError):
    """An admitted directory component no longer has its admitted name."""


# A symlinked final component is refused with ELOOP (Linux/macOS), EMLINK
# (FreeBSD) or ENOTDIR (O_DIRECTORY checked first).
_REPLACED_COMPONENT_ERRORS = {errno.ELOOP, errno.EMLINK, errno.ENOTDIR}
# Ancestors need only search permission, as pathname traversal did.
_SEARCH_ONLY = getattr(os, "O_PATH", 0) or getattr(os, "O_SEARCH", 0)


def _open_component(name, parent, *, search_only_ok):
    flags = os.O_DIRECTORY | os.O_NOFOLLOW
    try:
        child = os.open(name, os.O_RDONLY | flags, dir_fd=parent)
    except PermissionError:
        if not (search_only_ok and _SEARCH_ONLY):
            raise
        child = os.open(name, _SEARCH_ONLY | flags, dir_fd=parent)
    try:
        # O_PATH|O_NOFOLLOW may open a link itself; admit directories only.
        if not stat.S_ISDIR(os.fstat(child).st_mode):
            raise NotADirectoryError(errno.ENOTDIR, os.strerror(errno.ENOTDIR))
    except BaseException:
        os.close(child)
        raise
    return child


@contextmanager
def admitted_directory(path):
    """Open each absolute component without following links; retain its identity.

    All writes use the final fd. The checker rejects a renamed/replaced root or
    ancestor even when a replacement happens to contain identical metadata.
    Ancestors that grant only search permission are opened search-only where the
    platform supports it; the final directory must be readable.
    """
    from pathlib import Path

    descriptors = []
    components = []
    try:
        try:
            descriptor = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            descriptors.append(descriptor)
            parts = Path(os.path.abspath(path)).parts[1:]
            for position, name in enumerate(parts):
                search_only_ok = position < len(parts) - 1
                try:
                    child = _open_component(
                        name, descriptor, search_only_ok=search_only_ok
                    )
                except FileNotFoundError:
                    try:
                        os.mkdir(name, dir_fd=descriptor)
                    except FileExistsError:
                        pass  # Created concurrently; reopen it without following.
                    child = _open_component(
                        name, descriptor, search_only_ok=search_only_ok
                    )
                components.append((descriptor, name, child))
                descriptors.append(child)
                descriptor = child
        except OSError as error:
            if error.errno in _REPLACED_COMPONENT_ERRORS:
                raise DirectoryIdentityChanged(
                    "Episode directory path has a symlinked, non-directory or "
                    "changed component; use a symlink-free path or restore the "
                    "directory layout"
                ) from None
            reason = os.strerror(error.errno) if error.errno else "unknown error"
            raise DirectoryAdmissionError(
                f"Episode directory cannot be opened or created ({reason})"
            ) from None

        def check():
            try:
                for parent, name, child in components:
                    opened = os.fstat(child)
                    named = os.stat(name, dir_fd=parent, follow_symlinks=False)
                    if not stat.S_ISDIR(named.st_mode) or (
                        opened.st_dev,
                        opened.st_ino,
                    ) != (named.st_dev, named.st_ino):
                        raise DirectoryIdentityChanged(
                            "Episode directory identity changed; restore the "
                            "directory layout before retrying"
                        )
            except OSError:
                raise DirectoryIdentityChanged(
                    "Episode directory identity changed; restore the directory "
                    "layout before retrying"
                ) from None

        check()
        yield descriptor, check
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)
