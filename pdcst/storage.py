"""Sibling temporary files with ordinary creation and replacement permissions."""

import os
import stat
import uuid
from contextlib import contextmanager


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
    finally:
        temporary.unlink(missing_ok=True)
