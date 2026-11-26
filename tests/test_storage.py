"""Atomic file publication contracts with synthetic, injected OS failures."""

import errno
import os
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

from pdcst import storage


@pytest.mark.parametrize("binary", [False, True])
@pytest.mark.parametrize("existing", [False, True])
def test_payload_stays_private_until_publication(
    tmp_path, monkeypatch, binary, existing
):
    target = tmp_path / "saved"
    if existing:
        target.write_bytes(b"old")
        target.chmod(0o600)
    original_fdopen = storage.os.fdopen

    def fdopen(descriptor, *args, **kwargs):
        # Check before a caller can write even the first byte.
        assert stat.S_IMODE(os.fstat(descriptor).st_mode) & 0o077 == 0
        return original_fdopen(descriptor, *args, **kwargs)

    monkeypatch.setattr(storage.os, "fdopen", fdopen)
    previous = os.umask(0o022)
    try:
        with storage.replacement_file(target, binary=binary) as output:
            output.write(
                b"private synthetic URL" if binary else "private synthetic URL"
            )
            output.flush()
            (staging,) = tmp_path.glob(".*.tmp")
            assert stat.S_IMODE(staging.stat().st_mode) & 0o077 == 0
            assert staging.read_bytes() == b"private synthetic URL"
        assert stat.S_IMODE(target.stat().st_mode) == (0o600 if existing else 0o644)
    finally:
        os.umask(previous)
    assert not list(tmp_path.glob(".*.tmp"))


@pytest.mark.parametrize("relative", [False, True])
@pytest.mark.parametrize("kind", ["symlink", "fifo"])
def test_replacement_refuses_nonregular_destination(tmp_path, relative, kind):
    target = tmp_path / "saved"
    original = tmp_path / "original"
    original.write_bytes(b"previous audio")
    directory_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY) if relative else None
    try:
        with pytest.raises(OSError, match="Replacement target must be a regular file"):
            with storage.replacement_file(
                target, binary=True, directory_fd=directory_fd
            ) as output:
                output.write(b"new audio")
                if kind == "symlink":
                    target.symlink_to(original.name)
                else:
                    os.mkfifo(target)
    finally:
        if directory_fd is not None:
            os.close(directory_fd)
    is_expected_type = stat.S_ISLNK if kind == "symlink" else stat.S_ISFIFO
    assert is_expected_type(target.lstat().st_mode)
    assert original.read_bytes() == b"previous audio"
    assert set(tmp_path.iterdir()) == {target, original}


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
    original_lstat = Path.lstat
    original_replace = storage.os.replace

    def open_file(path, flags, mode=0o777):
        descriptor = original_open(path, flags, mode)
        if not flags & storage.os.O_DIRECTORY:
            requested_modes.append(mode)
            opened.append(descriptor)
        return descriptor

    def target_lstat(path):
        if path == target and existing:
            return SimpleNamespace(st_mode=stat.S_IFREG | 0o640)
        return original_lstat(path)

    def replace(source, destination):
        assert synced == opened
        # The stream must already be closed when publication begins.
        with pytest.raises(OSError) as error:
            storage.os.fstat(opened[0])
        assert error.value.errno == errno.EBADF
        original_replace(source, destination)

    monkeypatch.setattr(storage.os, "open", open_file)
    monkeypatch.setattr(Path, "lstat", target_lstat)
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
    assert preserved_modes[0] == 0o600
    assert len(preserved_modes) == 2
    if existing:
        assert preserved_modes[1] == 0o640
    assert sentinel.read_bytes() == b"unrelated"
    assert set(tmp_path.iterdir()) == {target, sentinel}


@pytest.mark.parametrize("relative", [False, True])
@pytest.mark.parametrize("replacement", ["file", "symlink"])
def test_replacement_refuses_exchanged_stage(tmp_path, relative, replacement):
    target = tmp_path / "saved"
    target.write_bytes(b"original")
    moved = tmp_path / "completed-stage"

    def exchange_stage():
        (stage,) = tmp_path.glob(".*.tmp")
        stage.rename(moved)
        if replacement == "symlink":
            stage.symlink_to(moved.name)
        else:
            stage.write_bytes(b"unrelated")

    directory_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY) if relative else None
    destination = tmp_path / "unused" / target.name if relative else target
    try:
        with pytest.raises(OSError, match="Replacement staging file identity changed"):
            with storage.replacement_file(
                destination,
                binary=True,
                directory_fd=directory_fd,
                before_replace=exchange_stage,
            ) as output:
                output.write(b"new audio")
    finally:
        if directory_fd is not None:
            os.close(directory_fd)

    assert target.read_bytes() == b"original"
    assert moved.read_bytes() == b"new audio"


@pytest.mark.parametrize("binary", [False, True])
@pytest.mark.parametrize(
    "failure",
    [
        "fdopen",
        "fdopen_after_open",
        "flush",
        "chmod",
        "publish_chmod",
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
    original_fchmod = storage.os.fchmod

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
    if failure == "publish_chmod":
        chmod_calls = 0

        def fchmod(fd, mode):
            nonlocal chmod_calls
            chmod_calls += 1
            if chmod_calls == 2:
                fail()
            original_fchmod(fd, mode)

        monkeypatch.setattr(storage.os, "fchmod", fchmod)
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


@pytest.mark.parametrize("relative", [False, True])
@pytest.mark.parametrize("exchanged", [False, True])
def test_failed_replacement_only_retires_owned_stage(tmp_path, relative, exchanged):
    target = tmp_path / "saved"
    target.write_bytes(b"original")
    moved = tmp_path / "moved-stage"
    primary = OSError(errno.EIO, "synthetic publication failure")

    def fail_publication():
        (stage,) = tmp_path.glob(".*.tmp")
        if exchanged:
            stage.rename(moved)
            stage.write_bytes(b"unrelated")
        raise primary

    directory_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY) if relative else None
    destination = tmp_path / "unused" / target.name if relative else target
    try:
        with pytest.raises(OSError) as raised:
            with storage.replacement_file(
                destination,
                binary=True,
                directory_fd=directory_fd,
                before_replace=fail_publication,
            ) as output:
                output.write(b"new audio")
    finally:
        if directory_fd is not None:
            os.close(directory_fd)

    assert raised.value is primary
    assert target.read_bytes() == b"original"
    if exchanged:
        (stage,) = tmp_path.glob(".*.tmp")
        assert stage.read_bytes() == b"unrelated"
        assert moved.read_bytes() == b"new audio"
    else:
        assert set(tmp_path.iterdir()) == {target}


def test_failed_replacement_preserves_primary_if_stage_stat_fails(
    tmp_path, monkeypatch
):
    target = tmp_path / "saved"
    target.write_bytes(b"original")
    primary = OSError(errno.EIO, "synthetic publication failure")
    original_lstat = Path.lstat

    def fail_stage_stat(path):
        if path.suffix == ".tmp":
            raise PermissionError("synthetic staging inspection failure")
        return original_lstat(path)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "lstat", fail_stage_stat)
        with pytest.raises(OSError) as raised:
            with storage.replacement_file(target, binary=True) as output:
                output.write(b"new audio")
                raise primary

    assert raised.value is primary
    assert target.read_bytes() == b"original"
    (stage,) = tmp_path.glob(".*.tmp")
    assert stage.read_bytes() == b"new audio"


@pytest.mark.parametrize("relative", [False, True])
@pytest.mark.parametrize("outcome", ["body failure", "sync failure", "success"])
def test_stage_cleanup_preserves_publication_result(
    tmp_path, monkeypatch, relative, outcome
):
    target = tmp_path / "saved"
    target.write_text("original")
    body_error = ValueError("synthetic body failure")
    unlinks = []

    def fail_unlink(path, *, dir_fd=None):
        unlinks.append((Path(path).suffix, dir_fd))
        raise PermissionError("synthetic unlink failure")

    def fail_sync(*args, **kwargs):
        raise OSError("synthetic sync failure")

    directory_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY) if relative else None

    def publish():
        with storage.replacement_file(target, directory_fd=directory_fd) as output:
            output.write("new")
            if outcome == "body failure":
                raise body_error

    try:
        with monkeypatch.context() as patch:
            patch.setattr(Path, "unlink", fail_unlink)
            patch.setattr(storage.os, "unlink", fail_unlink)
            if outcome == "body failure":
                with pytest.raises(ValueError) as caught:
                    publish()
                assert caught.value is body_error
            elif outcome == "sync failure":
                patch.setattr(storage, "sync_directory", fail_sync)
                with pytest.raises(storage.PublicationDurabilityError):
                    publish()
            else:
                publish()
    finally:
        if directory_fd is not None:
            os.close(directory_fd)

    if outcome == "body failure":
        assert unlinks == [(".tmp", directory_fd)]
        assert len(list(tmp_path.glob(".*.tmp"))) == 1
        assert target.read_text() == "original"
    else:
        assert not unlinks
        assert not list(tmp_path.glob(".*.tmp"))
        assert target.read_text() == "new"


@pytest.mark.parametrize("primary_failure", [None, "producer", "fsync"])
def test_descriptor_close_failure_preserves_primary_error(
    tmp_path, monkeypatch, primary_failure
):
    target = tmp_path / "episodes.json"
    target.write_bytes(b"[]")
    close_error = OSError(errno.EIO, "synthetic close failure")
    primary_error = OSError(errno.ENOSPC, "synthetic publication failure")
    original_close = storage.os.close
    closed = []

    def fail_close(descriptor):
        original_close(descriptor)
        closed.append(descriptor)
        raise close_error

    def fail_fsync(descriptor):
        raise primary_error

    with monkeypatch.context() as patch:
        patch.setattr(storage.os, "close", fail_close)
        if primary_failure == "fsync":
            patch.setattr(storage.os, "fsync", fail_fsync)
        with pytest.raises(OSError) as raised:
            with storage.replacement_file(target, binary=True) as output:
                output.write(b'[{"synthetic": true}]')
                if primary_failure == "producer":
                    raise primary_error

    assert raised.value is (primary_error if primary_failure else close_error)
    assert len(closed) == 1
    assert target.read_bytes() == b"[]"
    assert set(tmp_path.iterdir()) == {target}
