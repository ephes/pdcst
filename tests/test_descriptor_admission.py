"""Synthetic metadata and lock admission, including real process serialization."""

import hashlib
import os
import subprocess
import sys
import warnings
from pathlib import Path

import pytest

import pdcst
from pdcst.repository import EpisodeRepository, PodcastRepository, StoreReadError


@pytest.mark.parametrize("dangling", [False, True])
@pytest.mark.parametrize("kind", ["podcast", "episode"])
def test_metadata_links_refused_by_browse_and_refresh(
    tmp_path, monkeypatch, podcast, episode, dangling, kind
):
    monkeypatch.setattr(pdcst.settings, "root", tmp_path)
    home = tmp_path / "home"
    monkeypatch.setattr(pdcst.settings, "root", home)
    podcasts = PodcastRepository(home)
    podcasts.add(podcast)
    episodes = EpisodeRepository(home / podcast.directory)
    episodes.add(episode)
    repo = podcasts if kind == "podcast" else episodes
    original = repo.path.read_bytes()
    external = tmp_path / "external.json"
    if not dangling:
        external.write_bytes(original)
    repo.path.unlink()
    repo.path.symlink_to(external)

    class Parser:
        def get(self, *_):
            return podcast, [episode]

    with pytest.raises(StoreReadError):
        (
            pdcst.list_all_podcasts()
            if kind == "podcast"
            else pdcst.list_all_episodes(podcast)
        )
    with pytest.raises(StoreReadError):
        pdcst.subscribe(podcast.feed.url, "{title}", None, Parser(), podcasts)
    assert repo.path.is_symlink()
    assert (external.read_bytes() if external.exists() else None) == (
        None if dangling else original
    )


LOCK_SCRIPT = """
import sys
from pdcst.repository import EpisodeRepository, PodcastRepository
root, kind, mode = sys.argv[1:]
episodes = EpisodeRepository(root)
if kind == "metadata":
    context = episodes._lock()
elif kind == "feed":
    context = PodcastRepository(root).feed_lock("https://example.test/feed")
else:
    from types import SimpleNamespace
    from contextlib import contextmanager
    @contextmanager
    def download():
        def transfer(url, stage, **kwargs):
            print("acquired", flush=True)
            if mode == "hold":
                sys.stdin.readline()
            stage.write_bytes(b"synthetic audio")
        episodes.download_episode(
            SimpleNamespace(guid="synthetic", audio_url="https://example.test/audio"),
            "saved.mp3", transfer)
        yield
    context = download()
try:
    with context:
        if kind != "download":
            print("acquired", flush=True)
            if mode == "hold":
                sys.stdin.readline()
except ValueError as error:
    print(str(error), flush=True)
    sys.exit(2)
"""


def lock_path(root, kind):
    if kind == "feed":
        return (
            root
            / ".feed-locks"
            / (hashlib.sha256(b"https://example.test/feed").hexdigest() + ".lock")
        )
    return root / (
        "episodes.json.lock" if kind == "metadata" else "episodes.json.download.lock"
    )


def process(root, kind, mode, wait_seconds="30"):
    return subprocess.Popen(
        [sys.executable, "-c", LOCK_SCRIPT, str(root), kind, mode],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=dict(
            os.environ,
            PYTHONPATH=str(Path(pdcst.__file__).parent.parent),
            PDCST_LOCK_WAIT_SECONDS=wait_seconds,
        ),
    )


@pytest.mark.parametrize("kind", ["metadata", "download", "feed"])
@pytest.mark.parametrize("special", ["symlink", "fifo"])
def test_lock_special_files_refused_promptly(tmp_path, kind, special):
    path = lock_path(tmp_path, kind)
    path.parent.mkdir(parents=True, exist_ok=True)
    external = tmp_path / "external"
    external.write_bytes(b"untouched")
    if special == "symlink":
        path.symlink_to(external)
    else:
        os.mkfifo(path)
    child = process(tmp_path, kind, "once")
    try:
        out, err = child.communicate(timeout=5)
    finally:
        if child.poll() is None:
            child.kill()
            child.communicate()
    assert child.returncode == 2, (out, err)
    assert "Cannot acquire lock" in out
    assert external.read_bytes() == b"untouched"


@pytest.mark.parametrize("kind", ["metadata", "download", "feed"])
def test_two_processes_serialize_on_stable_lock(tmp_path, kind, episode):
    import select

    if kind == "download":
        episode.guid = "synthetic"
        episode.audio_url = "https://example.test/audio"
        EpisodeRepository(tmp_path).add(episode)
    first = process(tmp_path, kind, "hold")
    second = None
    try:
        assert select.select([first.stdout], [], [], 5)[0]
        assert first.stdout.readline().strip() == "acquired"
        inode = lock_path(tmp_path, kind).stat().st_ino
        second = process(tmp_path, kind, "once")
        assert not select.select([second.stdout], [], [], 0.3)[0]
        first.communicate("release\n", timeout=5)
        out, err = second.communicate(timeout=5)
        assert first.returncode == second.returncode == 0, err
        assert out.strip() == "acquired"
        assert lock_path(tmp_path, kind).stat().st_ino == inode
    finally:
        for child in (first, second):
            if child is not None and child.poll() is None:
                child.kill()
                child.communicate()


@pytest.mark.parametrize("kind", ["metadata", "download", "feed"])
def test_lock_contention_has_deadline(tmp_path, kind, episode):
    import select

    if kind == "download":
        episode.guid = "synthetic"
        episode.audio_url = "https://example.test/audio"
        EpisodeRepository(tmp_path).add(episode)
    holder = process(tmp_path, kind, "hold")
    contender = None
    try:
        assert select.select([holder.stdout], [], [], 5)[0]
        assert holder.stdout.readline().strip() == "acquired"
        inode = lock_path(tmp_path, kind).stat().st_ino
        contender = process(tmp_path, kind, "once", wait_seconds="0.2")
        out, err = contender.communicate(timeout=3)
        assert contender.returncode == 2, err
        assert "busy" in out and "retry" in out
        assert "acquired" not in out
        holder.communicate("release\n", timeout=5)
        assert lock_path(tmp_path, kind).stat().st_ino == inode
        retry = process(tmp_path, kind, "once", wait_seconds="0.2")
        out, err = retry.communicate(timeout=5)
        assert retry.returncode == 0, err
        assert out.strip() == "acquired"
    finally:
        for child in (holder, contender):
            if child is not None and child.poll() is None:
                child.kill()
                child.communicate()


def test_interrupted_lock_wait_closes_descriptor(tmp_path, monkeypatch):
    import pdcst.repository as module

    descriptors = []
    real_close = os.close

    def contended(descriptor, operation):
        assert operation & module.fcntl.LOCK_NB
        descriptors.append(descriptor)
        raise BlockingIOError("synthetic contention")

    def interrupt(_):
        raise KeyboardInterrupt

    closed = []

    def close(descriptor):
        closed.append(descriptor)
        real_close(descriptor)

    monkeypatch.setattr(module.fcntl, "flock", contended)
    monkeypatch.setattr(module.time, "sleep", interrupt)
    monkeypatch.setattr(module.os, "close", close)
    with pytest.raises(KeyboardInterrupt):
        with module.advisory_lock(tmp_path / "test.lock"):
            pytest.fail("must not acquire contended lock")
    assert closed == descriptors


@pytest.mark.parametrize(
    "outcome", ["refused", "failed", "completed", "warnings-as-errors"]
)
def test_lock_close_failure_preserves_catalog_outcome(tmp_path, monkeypatch, outcome):
    import pdcst.repository as module

    lock = tmp_path / "podcasts.json.lock"
    catalog = tmp_path / "podcasts.json"
    attempts = []
    real_close = os.close
    failure = ValueError("synthetic catalog publication failure")

    def failing_close(descriptor):
        attempts.append(descriptor)
        real_close(descriptor)
        raise OSError("synthetic close failure")

    def publish():
        with module.advisory_lock(lock):
            if outcome == "failed":
                raise failure
            assert outcome in {"completed", "warnings-as-errors"}
            catalog.write_bytes(b"[]")

    monkeypatch.setattr(module.os, "close", failing_close)
    if outcome == "refused":

        def refuse(*args):
            raise OSError("synthetic flock failure")

        monkeypatch.setattr(module.fcntl, "flock", refuse)
        with pytest.raises(
            module.StoreWriteError, match="regular stable file required"
        ):
            publish()
    elif outcome == "failed":
        with pytest.raises(ValueError) as raised:
            publish()
        assert raised.value is failure
    elif outcome == "warnings-as-errors":
        with warnings.catch_warnings():
            warnings.simplefilter("error", RuntimeWarning)
            publish()
        assert catalog.read_bytes() == b"[]"
    else:
        with pytest.warns(RuntimeWarning, match="lock release uncertain"):
            publish()
        assert catalog.read_bytes() == b"[]"
    assert len(attempts) == 1


@pytest.mark.parametrize("delay_at", ["flock", "stat"])
def test_catalog_lock_refuses_late_admission(tmp_path, monkeypatch, delay_at):
    import pdcst.repository as module

    path = tmp_path / "podcasts.json.lock"
    now = 100.0
    descriptors = []
    closed = []
    real_flock = module.fcntl.flock
    real_stat = os.stat
    real_close = os.close

    def flock(descriptor, operation):
        nonlocal now
        real_flock(descriptor, operation)
        descriptors.append(descriptor)
        if delay_at == "flock":
            now = 101.0

    def stat(entry, *args, **kwargs):
        nonlocal now
        info = real_stat(entry, *args, **kwargs)
        if entry == path and delay_at == "stat":
            now = 101.0
        return info

    def close(descriptor):
        closed.append(descriptor)
        real_close(descriptor)

    monkeypatch.setattr(module.settings, "lock_wait_seconds", 1.0)
    monkeypatch.setattr(module.time, "monotonic", lambda: now)
    monkeypatch.setattr(module.fcntl, "flock", flock)
    monkeypatch.setattr(module.os, "stat", stat)
    monkeypatch.setattr(module.os, "close", close)
    with pytest.raises(module.StoreWriteError, match="busy .*retry later"):
        with module.advisory_lock(path):
            pytest.fail("catalog mutation started after its admission deadline")
    assert len(descriptors) == 1
    assert closed == descriptors


@pytest.mark.parametrize("value", [0, -1, float("nan"), float("inf")])
def test_lock_wait_configuration_rejects_invalid_duration(tmp_path, value):
    from pydantic import ValidationError

    from pdcst.config import Settings

    with pytest.raises(ValidationError):
        Settings(root=tmp_path, lock_wait_seconds=value)


def test_lock_wait_configuration_environment(tmp_path, monkeypatch):
    from pdcst.config import Settings

    monkeypatch.setenv("PDCST_LOCK_WAIT_SECONDS", "0.25")
    assert Settings(root=tmp_path).lock_wait_seconds == 0.25


@pytest.mark.parametrize("source", ["constructor", "environment", "assignment"])
def test_lock_wait_configuration_rejects_largest_finite_duration(
    tmp_path, monkeypatch, source
):
    from pydantic import ValidationError

    from pdcst.config import Settings

    value = sys.float_info.max
    if source == "constructor":
        with pytest.raises(ValidationError, match="lock_wait_seconds"):
            Settings(root=tmp_path, lock_wait_seconds=value)
    elif source == "environment":
        monkeypatch.setenv("PDCST_LOCK_WAIT_SECONDS", str(value))
        with pytest.raises(ValidationError, match="lock_wait_seconds"):
            Settings(root=tmp_path)
    else:
        configured = Settings(root=tmp_path, lock_wait_seconds=0.25)
        with pytest.raises(ValidationError, match="lock_wait_seconds"):
            configured.lock_wait_seconds = value
        assert configured.lock_wait_seconds == 0.25


def test_lock_wait_accepts_upper_bound(tmp_path):
    from pdcst.config import Settings

    assert Settings(root=tmp_path, lock_wait_seconds=86_400).lock_wait_seconds == 86_400


def test_fractional_lock_wait_under_contention(tmp_path, monkeypatch):
    import pdcst.repository as module
    from pdcst.config import Settings

    monkeypatch.setenv("PDCST_LOCK_WAIT_SECONDS", "0.125")
    monkeypatch.setattr(module, "settings", Settings(root=tmp_path))
    sleeps = []
    real_flock = module.fcntl.flock

    def flock(descriptor, operation):
        if not sleeps:
            raise BlockingIOError("synthetic contention")
        real_flock(descriptor, operation)

    monkeypatch.setattr(module.fcntl, "flock", flock)
    monkeypatch.setattr(module.time, "monotonic", lambda: sum(sleeps))
    monkeypatch.setattr(module.time, "sleep", sleeps.append)
    with module.advisory_lock(tmp_path / "podcasts.json.lock"):
        assert sleeps == [0.05]
