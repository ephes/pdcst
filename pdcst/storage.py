"""Sibling temporary files with ordinary creation and replacement permissions."""

import errno
import os
import stat
import uuid
from contextlib import contextmanager


class InvalidReplacementTarget(OSError):
    """The destination exists and is not a regular file."""


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
def replacement_file(target, *, binary=False, directory_fd=None, before_replace=None):
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
                    info = (
                        target.lstat()
                        if directory_fd is None
                        else os.stat(
                            target.name, dir_fd=directory_fd, follow_symlinks=False
                        )
                    )
                except FileNotFoundError:
                    mode = creation_mode
                else:
                    if not stat.S_ISREG(info.st_mode):
                        raise InvalidReplacementTarget(
                            "Replacement target must be a regular file"
                        )
                    mode = stat.S_IMODE(info.st_mode)
                os.fchmod(output.fileno(), mode)
                os.fsync(output.fileno())
        except BaseException:
            try:
                os.close(descriptor)
            except OSError:
                pass  # Preserve the original staging failure.
            raise
        else:
            os.close(descriptor)
        if before_replace is not None:
            before_replace()
        if directory_fd is None:
            os.replace(temporary, target)
        else:
            os.replace(
                local(temporary),
                local(target),
                src_dir_fd=directory_fd,
                dst_dir_fd=directory_fd,
            )
    except BaseException:
        # Only failures before replacement can leave the owned staging name.
        try:
            if directory_fd is None:
                temporary.unlink()
            else:
                os.unlink(temporary.name, dir_fd=directory_fd)
        except OSError:
            pass  # Preserve the original publication failure.
        raise
    try:
        if directory_fd is None:
            sync_directory(target.parent)
        else:
            sync_directory(target.parent, directory_fd=directory_fd)
    except (OSError, KeyboardInterrupt):
        raise PublicationDurabilityError(target) from None


class DirectoryAdmissionError(ValueError):
    """A directory component cannot be admitted or has been replaced."""


class DirectoryIdentityChanged(DirectoryAdmissionError):
    """An admitted directory component no longer has its admitted name."""


# A symlinked final component is refused with ELOOP (Linux/macOS), EMLINK
# (FreeBSD) or ENOTDIR (O_DIRECTORY checked first).
_REPLACED_COMPONENT_ERRORS = {errno.ELOOP, errno.EMLINK, errno.ENOTDIR}
_UNSYNCABLE_DIRECTORY = {errno.EINVAL, errno.ENOTSUP, errno.EOPNOTSUPP, errno.EROFS}
# Ancestors need only search permission, as pathname traversal did.
_SEARCH_ONLY = getattr(os, "O_PATH", 0) or getattr(os, "O_SEARCH", 0)


def _open_component(name, parent, *, search_only_ok):
    flags = os.O_DIRECTORY | os.O_NOFOLLOW
    readable = True
    try:
        child = os.open(name, os.O_RDONLY | flags, dir_fd=parent)
    except PermissionError:
        if not (search_only_ok and _SEARCH_ONLY):
            raise
        child = os.open(name, _SEARCH_ONLY | flags, dir_fd=parent)
        readable = False
    try:
        # O_PATH|O_NOFOLLOW may open a link itself; admit directories only.
        if not stat.S_ISDIR(os.fstat(child).st_mode):
            raise NotADirectoryError(errno.ENOTDIR, os.strerror(errno.ENOTDIR))
    except BaseException:
        os.close(child)
        raise
    return child, readable


@contextmanager
def admitted_directory(
    path,
    *,
    create=True,
    parent_fd=None,
    label="Episode directory",
    created_below=None,
):
    """Open components without following links and retain their identities.

    Relative paths may start at a borrowed parent_fd; its owner must also check
    that parent's identity. Missing components are created only with create=True.
    All writes use the final fd. The checker rejects a renamed/replaced root or
    ancestor even when a replacement happens to contain identical metadata.
    Ancestors that grant only search permission are opened search-only where the
    platform supports it. Creation requires a readable parent and syncs each
    parent link, including readable parents of already existing components so
    a retry can settle an interrupted creation. The final directory must be
    readable. The label names the admitted directory in error messages.
    created_below names an absolute directory below which the caller has just
    created the components itself; their parent syncs are never tolerated.
    """
    from pathlib import Path

    descriptors = []
    components = []
    try:
        try:
            if parent_fd is None:
                descriptor = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                parts = Path(os.path.abspath(path)).parts[1:]
            else:
                relative = Path(path)
                if (
                    relative.is_absolute()
                    or ".." in relative.parts
                    or not relative.parts
                ):
                    raise DirectoryAdmissionError(
                        "Directory must be a relative child path"
                    )
                descriptor = os.dup(parent_fd)
                parts = relative.parts
            descriptors.append(descriptor)
            readable = True
            new_from = len(parts)
            if created_below is not None:
                base = Path(os.path.abspath(created_below)).parts[1:]
                new_from = len(base) if tuple(parts[: len(base)]) == base else 0
            for position, name in enumerate(parts):
                search_only_ok = position < len(parts) - 1
                created = position >= new_from
                try:
                    child, child_readable = _open_component(
                        name, descriptor, search_only_ok=search_only_ok
                    )
                except FileNotFoundError:
                    if not create:
                        raise
                    if not readable:
                        raise DirectoryAdmissionError(
                            f"{label} cannot be created below a search-only parent"
                        ) from None
                    created = True
                    try:
                        os.mkdir(name, dir_fd=descriptor)
                    except FileExistsError:
                        pass  # Created concurrently; reopen it without following.
                    child, child_readable = _open_component(
                        name, descriptor, search_only_ok=search_only_ok
                    )
                components.append((descriptor, name, child))
                descriptors.append(child)
                if create and readable:
                    # Existence alone cannot distinguish a durable link from
                    # a mkdir left behind by a failed sync or another writer.
                    try:
                        sync_directory(None, directory_fd=descriptor)
                    except OSError as error:
                        # Read-only or sync-less ancestors (squashfs, erofs)
                        # cannot persist any link; only fail for a new one.
                        if created or error.errno not in _UNSYNCABLE_DIRECTORY:
                            raise DirectoryAdmissionError(
                                f"{label}: parent directory sync failed; "
                                "retry the operation"
                            ) from None
                descriptor = child
                readable = child_readable
        except OSError as error:
            if error.errno in _REPLACED_COMPONENT_ERRORS:
                raise DirectoryIdentityChanged(
                    f"{label} path has a symlinked, non-directory or "
                    "changed component; use a symlink-free path or restore the "
                    "directory layout"
                ) from None
            reason = os.strerror(error.errno) if error.errno else "unknown error"
            raise DirectoryAdmissionError(
                f"{label} cannot be opened or created ({reason})"
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
                            f"{label} identity changed; restore the "
                            "directory layout before retrying"
                        )
            except OSError:
                raise DirectoryIdentityChanged(
                    f"{label} identity changed; restore the directory "
                    "layout before retrying"
                ) from None

        check()
        yield descriptor, check
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)
