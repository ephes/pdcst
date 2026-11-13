"""Sibling temporary files with ordinary creation and replacement permissions."""

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


def sync_directory(path):
    """Persist preceding name changes before reporting durable publication."""
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


@contextmanager
def replacement_file(target, *, binary=False):
    # Short name: independent of the length of the destination name.
    temporary = target.with_name(f".{uuid.uuid4().hex}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)
    try:
        # Keep descriptor ownership here even if stream construction fails midway.
        try:
            with os.fdopen(
                descriptor,
                "wb" if binary else "w",
                closefd=False,
                **({} if binary else {"encoding": "utf-8"}),
            ) as output:
                yield output
                output.flush()
                try:
                    mode = stat.S_IMODE(target.stat().st_mode)
                except FileNotFoundError:
                    pass
                else:
                    os.fchmod(output.fileno(), mode)
                os.fsync(output.fileno())
        finally:
            os.close(descriptor)
        os.replace(temporary, target)
        try:
            sync_directory(target.parent)
        except (OSError, KeyboardInterrupt):
            raise PublicationDurabilityError(target) from None
    finally:
        temporary.unlink(missing_ok=True)
