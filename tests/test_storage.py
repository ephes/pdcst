"""Atomic file publication contracts with synthetic, injected OS failures."""

import errno
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

from pdcst import storage


@pytest.mark.parametrize("binary", [False, True])
@pytest.mark.parametrize("existing", [False, True])
def test_replacement_publishes_after_close(tmp_path, monkeypatch, binary, existing):
    target = tmp_path / "saved"
    if existing:
        target.write_bytes(b"original")
    sentinel = tmp_path / ".unrelated.tmp"
    sentinel.write_bytes(b"unrelated")
    requested_modes = []
    preserved_modes = []
    synced = []
    opened = []
    original_open = storage.os.open
    original_stat = Path.stat
    original_replace = storage.os.replace

    def open_file(path, flags, mode=0o777):
        descriptor = original_open(path, flags, mode)
        if not flags & storage.os.O_DIRECTORY:
            requested_modes.append(mode)
            opened.append(descriptor)
        return descriptor

    def target_stat(path, *args, **kwargs):
        if path == target and existing:
            return SimpleNamespace(st_mode=stat.S_IFREG | 0o640)
        return original_stat(path, *args, **kwargs)

    def replace(source, destination):
        assert synced == opened
        # The stream must already be closed when publication begins.
        with pytest.raises(OSError) as error:
            storage.os.fstat(opened[0])
        assert error.value.errno == errno.EBADF
        original_replace(source, destination)

    monkeypatch.setattr(storage.os, "open", open_file)
    monkeypatch.setattr(Path, "stat", target_stat)
    monkeypatch.setattr(
        storage.os, "fchmod", lambda fd, mode: preserved_modes.append(mode)
    )
    monkeypatch.setattr(storage.os, "fsync", lambda fd: synced.append(fd))
    monkeypatch.setattr(storage.os, "replace", replace)
    data = b"\x00\xffsynthetic" if binary else "Unicode \u00e9\U0001f40d\n"
    with storage.replacement_file(target, binary=binary) as output:
        output.write(data)
        if existing:
            assert target.read_bytes() == b"original"
        else:
            assert not target.exists()
    assert target.read_bytes() == (data if binary else data.encode("utf-8"))
    assert requested_modes == [0o666]
    assert preserved_modes == ([0o640] if existing else [])
    assert sentinel.read_bytes() == b"unrelated"
    assert set(tmp_path.iterdir()) == {target, sentinel}


@pytest.mark.parametrize("binary", [False, True])
@pytest.mark.parametrize(
    "failure",
    [
        "fdopen",
        "fdopen_after_open",
        "flush",
        "chmod",
        "fsync",
        "close",
        "replace",
        "body",
        "interrupt",
    ],
)
def test_failure_preserves_target_and_closes_descriptor(
    tmp_path, monkeypatch, binary, failure
):
    target = tmp_path / "saved"
    target.write_bytes(b"original")
    original_mode = target.stat().st_mode
    sentinel = tmp_path / ".unrelated.tmp"
    sentinel.write_bytes(b"unrelated")
    opened = []
    injected = []
    original_open = storage.os.open
    original_fdopen = storage.os.fdopen

    def fail():
        injected.append(failure)
        raise OSError("synthetic " + failure)

    def open_file(*args):
        descriptor = original_open(*args)
        opened.append(descriptor)
        return descriptor

    class Stream:
        def __init__(self, stream):
            self.stream = stream

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.stream.__exit__(*args)
            if failure == "close":
                fail()

        def write(self, data):
            return self.stream.write(data)

        def flush(self):
            if failure == "flush":
                fail()
            self.stream.flush()

        def fileno(self):
            return self.stream.fileno()

    def fdopen(*args, **kwargs):
        if failure == "fdopen":
            fail()
        stream = original_fdopen(*args, **kwargs)
        if failure == "fdopen_after_open":
            stream.close()
            injected.append(failure)
            raise KeyboardInterrupt("synthetic " + failure)
        return Stream(stream)

    monkeypatch.setattr(storage.os, "open", open_file)
    monkeypatch.setattr(storage.os, "fdopen", fdopen)
    if failure in {"chmod", "fsync", "replace"}:
        name = "fchmod" if failure == "chmod" else failure
        monkeypatch.setattr(storage.os, name, lambda *args: fail())
    expected = (
        KeyboardInterrupt if failure in {"interrupt", "fdopen_after_open"} else OSError
    )
    with pytest.raises(expected, match="synthetic"):
        with storage.replacement_file(target, binary=binary) as output:
            output.write(b"new" if binary else "new")
            if failure == "body":
                fail()
            elif failure == "interrupt":
                injected.append(failure)
                raise KeyboardInterrupt("synthetic interrupt")
    assert injected == [failure]
    assert target.read_bytes() == b"original"
    assert target.stat().st_mode == original_mode
    assert sentinel.read_bytes() == b"unrelated"
    assert set(tmp_path.iterdir()) == {target, sentinel}
    assert len(opened) == 1
    try:
        storage.os.fstat(opened[0])
    except OSError as error:
        assert error.errno == errno.EBADF
    else:
        storage.os.close(opened[0])  # Avoid leaking even when this regression fails.
        pytest.fail("temporary descriptor leaked")


def test_exclusive_open_failure_does_not_remove_existing_temp(tmp_path, monkeypatch):
    target = tmp_path / "saved"
    target.write_bytes(b"original")
    monkeypatch.setattr(storage.uuid, "uuid4", lambda: SimpleNamespace(hex="fixed"))
    occupied = tmp_path / ".fixed.tmp"
    occupied.write_bytes(b"belongs to someone else")
    with pytest.raises(FileExistsError):
        with storage.replacement_file(target):
            pytest.fail("exclusive open should fail before yielding")
    assert occupied.read_bytes() == b"belongs to someone else"
    assert target.read_bytes() == b"original"
