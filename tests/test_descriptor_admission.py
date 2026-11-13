"""Synthetic metadata and lock admission, including real process serialization."""

import hashlib
import os
import subprocess
import sys
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
        def transfer(url, stage):
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


def process(root, kind, mode):
    return subprocess.Popen(
        [sys.executable, "-c", LOCK_SCRIPT, str(root), kind, mode],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=dict(os.environ, PYTHONPATH=str(Path(pdcst.__file__).parent.parent)),
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
