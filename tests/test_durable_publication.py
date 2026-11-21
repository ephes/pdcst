"""Real publication ordering and synthetic directory-fsync failure journeys."""

import copy
import importlib
import json
import os
import stat
from pathlib import Path

import httpx
import pytest

import pdcst
from pdcst import storage
from pdcst.download import DownloadError, download_with_progress
from pdcst.repository import EpisodeRepository, PodcastRepository


@pytest.mark.parametrize("fail_sync", [None, OSError, KeyboardInterrupt])
def test_replacement_syncs_directory_after_replace(tmp_path, monkeypatch, fail_sync):
    target = tmp_path / "saved"
    target.write_bytes(b"old")
    events = []
    sync = os.fsync
    replace = os.replace
    directory_fds = []

    def fsync(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            directory_fds.append(fd)
            events.append("directory")
            assert target.read_bytes() == b"new"
            if fail_sync:
                raise fail_sync("synthetic directory sync")
        else:
            events.append("file")
        sync(fd)

    def publish(source, destination):
        events.append("replace")
        replace(source, destination)

    monkeypatch.setattr(os, "fsync", fsync)
    monkeypatch.setattr(os, "replace", publish)

    def write():
        with storage.replacement_file(target, binary=True) as output:
            output.write(b"new")

    if fail_sync:
        with pytest.raises(OSError, match="durability uncertain"):
            write()
    else:
        write()
    assert events == ["file", "replace", "directory"]
    assert target.read_bytes() == b"new"
    assert list(tmp_path.iterdir()) == [target]
    for fd in directory_fds:
        with pytest.raises(OSError):
            os.fstat(fd)


@pytest.mark.parametrize("failure", [None, "audio", "metadata", "retirement"])
def test_download_syncs_publications_before_retiring_backup(
    tmp_path, monkeypatch, episode, failure
):
    repo = EpisodeRepository(tmp_path)
    episode.audio_file = "saved.mp3"
    repo.add(episode)
    target = tmp_path / episode.audio_file
    target.write_bytes(b"previous")
    old_json = repo.path.read_bytes()
    events = []
    sync = os.fsync
    unlink = os.unlink

    def fsync(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            if target.exists() and target.read_bytes() == b"completed":
                phase = "metadata" if repo.path.read_bytes() != old_json else "audio"
                # Change the JSON bytes on a successful link commit.
                events.append(phase)
                if failure == phase or (failure == "retirement" and "retire" in events):
                    raise OSError("synthetic directory sync")
        sync(fd)

    def retire(path, *args, **kwargs):
        path = Path(path)
        if path.name.endswith(".previous"):
            assert events[:2] == ["audio", "metadata"]
            events.append("retire")
        return unlink(path, *args, **kwargs)

    # Whitespace makes the link commit observable even when the link is unchanged.
    repo.path.write_bytes(b"\n" + old_json)
    old_json = repo.path.read_bytes()
    monkeypatch.setattr(os, "fsync", fsync)
    monkeypatch.setattr(os, "unlink", retire)

    def transfer(url, stage, **kwargs):
        stage.write_bytes(b"completed")

    if failure in {"audio", "metadata"}:
        with pytest.warns(RuntimeWarning):
            with pytest.raises(OSError):
                repo.download_episode(episode, target.name, transfer)
        assert "retire" not in events
        if failure == "audio":
            assert target.read_bytes() == b"previous"
            assert repo.path.read_bytes() == old_json
        else:
            assert target.read_bytes() == b"completed"
            from pdcst.repository import StoreReadError

            with pytest.raises(StoreReadError, match="unresolved publication"):
                repo.list()
            assert json.loads(repo.path.read_text())[0]["audio_file"] == target.name
            assert any(
                p.read_bytes() == b"previous" for p in tmp_path.glob("*.previous")
            )
    else:
        if failure == "retirement":
            with pytest.warns(RuntimeWarning, match="durably retire"):
                assert repo.download_episode(episode, target.name, transfer) == target
        else:
            assert repo.download_episode(episode, target.name, transfer) == target
        assert events == ["audio", "metadata", "metadata", "retire", "metadata"]
        assert not list(tmp_path.glob("*.previous"))


def test_refresh_directory_sync_failure_keeps_published_pair(
    tmp_path, monkeypatch, podcast, episode
):
    monkeypatch.setattr(pdcst.settings, "root", tmp_path)
    podcasts = PodcastRepository(tmp_path)
    podcasts.add(podcast)
    episodes = EpisodeRepository(tmp_path / podcast.directory)
    episodes.add(episode)
    fresh = copy.deepcopy(episode)
    fresh.title = "Refreshed"
    updated = copy.deepcopy(podcast)
    updated.episodes_count = 1
    sync = os.fsync

    def fsync(fd):
        if (
            stat.S_ISDIR(os.fstat(fd).st_mode)
            and json.loads(podcasts.path.read_text())[0]["episodes_count"] == 1
        ):
            raise OSError("synthetic directory sync")
        sync(fd)

    class Parser:
        def get(self, *_):
            return updated, [fresh]

    monkeypatch.setattr(os, "fsync", fsync)
    with pytest.raises(OSError, match="durability uncertain"):
        pdcst.subscribe(podcast.feed.url, "{title}", None, Parser(), podcasts)
    from pdcst.repository import StoreReadError

    assert json.loads(podcasts.path.read_text())[0]["episodes_count"] == 1
    assert json.loads(episodes.path.read_text())[0]["title"] == "Refreshed"
    for repo in (podcasts, episodes):
        with pytest.raises(StoreReadError, match="unresolved publication"):
            repo.list()


@pytest.mark.parametrize(
    "headers", [{"Content-Length": "0"}, {}, {"Transfer-Encoding": "chunked"}]
)
@pytest.mark.parametrize("existing", [False, True])
def test_empty_download_never_publishes(
    tmp_path, monkeypatch, episode, headers, existing
):
    repo = EpisodeRepository(tmp_path)
    target = tmp_path / "saved.mp3"
    if existing:
        episode.audio_file = target.name
        target.write_bytes(b"previous audio")
    repo.add(episode)
    before = repo.path.read_bytes()
    client = httpx.Client
    monkeypatch.setattr(
        importlib.import_module("pdcst.download").httpx,
        "Client",
        lambda **kw: client(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200, headers=headers, stream=httpx.ByteStream(b"")
                )
            ),
            **kw,
        ),
    )
    with pytest.raises(DownloadError, match="Empty audio response"):
        repo.download_episode(episode, target.name, download_with_progress)
    assert repo.path.read_bytes() == before
    assert (target.read_bytes() if target.exists() else None) == (
        b"previous audio" if existing else None
    )
    assert not list(tmp_path.glob(".pdcst-download-*"))
    assert not list(tmp_path.glob("*.tmp"))


def test_small_audio_download_publishes_real_bytes(tmp_path, monkeypatch, episode):
    import wave
    from io import BytesIO

    body = BytesIO()
    with wave.open(body, "wb") as audio:
        audio.setparams((1, 2, 8000, 0, "NONE", "not compressed"))
        audio.writeframes(b"\x00\x00" * 80)
    data = body.getvalue()
    client = httpx.Client
    monkeypatch.setattr(
        httpx,
        "Client",
        lambda **kw: client(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200,
                    headers={"Content-Length": str(len(data))},
                    stream=httpx.ByteStream(data),
                )
            ),
            **kw,
        ),
    )
    repo = EpisodeRepository(tmp_path)
    repo.add(episode)
    target = repo.download_episode(episode, "saved.wav", download_with_progress)
    assert target.read_bytes() == data
    assert repo.list()[0].audio_file == target.name


@pytest.mark.parametrize("use_alias", [False, True])
def test_direct_download_retries_unsynced_relative_parent(
    tmp_path, monkeypatch, use_alias
):
    root = tmp_path / "real"
    root.mkdir()
    if use_alias:
        (tmp_path / "alias").symlink_to(root, target_is_directory=True)
    monkeypatch.chdir(tmp_path)
    prefix = "alias" if use_alias else "real"
    target = Path(prefix) / "shows" / "episodes" / "saved.mp3"
    parent = root / "shows"
    events = []
    fail_sync = True
    fsync = os.fsync

    def sync(fd):
        info = os.fstat(fd)
        if os.path.samestat(info, root.stat()):
            events.append(("root", target.exists()))
            if fail_sync:
                raise OSError("synthetic parent sync failure")
        elif parent.exists() and os.path.samestat(info, parent.stat()):
            events.append(("parent", target.exists()))
        elif target.parent.exists() and os.path.samestat(info, target.parent.stat()):
            events.append(("leaf", target.exists()))
        fsync(fd)

    client = httpx.Client
    monkeypatch.setattr(
        httpx,
        "Client",
        lambda **kwargs: client(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200, stream=httpx.ByteStream(b"completed audio")
                )
            ),
            **kwargs,
        ),
    )
    monkeypatch.setattr(os, "fsync", sync)

    url = "https://example.com/audio.mp3"
    with pytest.raises(DownloadError, match="parent directory sync failed"):
        download_with_progress(url, target)
    assert not target.exists()
    assert events == [("root", False)]

    events.clear()
    fail_sync = False
    assert download_with_progress(url, target) == target
    assert events == [("root", False), ("parent", False), ("leaf", True)]
    assert (
        root / "shows" / "episodes" / target.name
    ).read_bytes() == b"completed audio"


def _serve(monkeypatch, content):
    client = httpx.Client
    monkeypatch.setattr(
        httpx,
        "Client",
        lambda **kwargs: client(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, content=content())
            ),
            **kwargs,
        ),
    )


@pytest.mark.skipif(
    not (getattr(os, "O_PATH", 0) or getattr(os, "O_SEARCH", 0)),
    reason="platform has no search-only directory descriptors",
)
@pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses directory permissions")
def test_direct_download_admits_existing_search_only_ancestor(tmp_path, monkeypatch):
    guarded = tmp_path / "guarded"
    destination = guarded / "shows"
    destination.mkdir(parents=True)
    target = destination / "saved.mp3"
    _serve(monkeypatch, lambda: iter([b"completed audio"]))
    guarded.chmod(0o311)
    try:
        assert download_with_progress("https://example.com/a.mp3", target) == target
    finally:
        guarded.chmod(0o755)
    assert target.read_bytes() == b"completed audio"


@pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses directory permissions")
def test_direct_download_refuses_new_link_below_unreadable_parent(
    tmp_path, monkeypatch
):
    guarded = tmp_path / "guarded"
    guarded.mkdir()
    target = guarded / "shows" / "saved.mp3"
    _serve(monkeypatch, lambda: iter([b"completed audio"]))
    guarded.chmod(0o311)
    try:
        with pytest.raises(DownloadError, match="unreadable parent"):
            download_with_progress("https://example.com/a.mp3", target)
        assert not target.parent.exists()
    finally:
        guarded.chmod(0o755)


def test_direct_download_reports_alias_change_as_download_error(tmp_path, monkeypatch):
    root = tmp_path / "real"
    root.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(root, target_is_directory=True)
    target = alias / "saved.mp3"

    def chunks():
        yield b"completed "
        alias.unlink()
        yield b"audio"

    _serve(monkeypatch, chunks)
    with pytest.raises(DownloadError, match="cannot be resolved") as raised:
        download_with_progress("https://example.com/a.mp3", target)
    assert str(tmp_path) not in str(raised.value)
    assert list(root.iterdir()) == []


@pytest.mark.parametrize("new_link", [False, True])
def test_direct_download_tolerates_unsyncable_ancestor_only_for_existing_links(
    tmp_path, monkeypatch, new_link
):
    import errno

    base = tmp_path / "base"
    base.mkdir()
    destination = base / "new"
    if not new_link:
        destination.mkdir()
    target = destination / "saved.mp3"
    fsync = os.fsync

    def sync(fd):
        if os.path.samestat(os.fstat(fd), base.stat()):
            raise OSError(errno.EINVAL, "directory fsync unsupported")
        fsync(fd)

    _serve(monkeypatch, lambda: iter([b"completed audio"]))
    monkeypatch.setattr(os, "fsync", sync)
    url = "https://example.com/a.mp3"
    if new_link:
        with pytest.raises(DownloadError, match="parent directory sync failed"):
            download_with_progress(url, target)
        assert not target.exists()
    else:
        assert download_with_progress(url, target) == target
        assert target.read_bytes() == b"completed audio"
