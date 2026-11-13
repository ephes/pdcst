"""Rolled-back refreshes and internal stages never report a visible publication."""

import copy
import os
import stat

import httpx
import pytest

import pdcst
from pdcst.download import DownloadError, download_with_progress
from pdcst.repository import EpisodeRepository, PodcastRepository, StoreWriteError
from pdcst.storage import PublicationDurabilityError


def _refresh_setup(tmp_path, monkeypatch, podcast, episode):
    monkeypatch.setattr(pdcst.settings, "root", tmp_path)
    podcasts = PodcastRepository(tmp_path)
    podcasts.add(podcast)
    episodes = EpisodeRepository(tmp_path / podcast.directory)
    episodes.add(episode)
    fresh = copy.deepcopy(episode)
    fresh.title = "Refreshed"
    updated = copy.deepcopy(podcast)
    updated.episodes_count = 1

    class Parser:
        def get(self, *_):
            return updated, [fresh]

    return podcasts, episodes, Parser()


def test_rolled_back_episode_sync_failure_is_not_reported_published(
    tmp_path, monkeypatch, podcast, episode
):
    podcasts, episodes, parser = _refresh_setup(tmp_path, monkeypatch, podcast, episode)
    episodes_before = episodes.path.read_bytes()
    podcasts_before = podcasts.path.read_bytes()
    sync = os.fsync
    failed = []

    def fsync(fd):
        if (
            not failed
            and stat.S_ISDIR(os.fstat(fd).st_mode)
            and b"Refreshed" in episodes.path.read_bytes()
        ):
            failed.append(True)
            raise OSError("synthetic directory sync")
        sync(fd)

    monkeypatch.setattr(os, "fsync", fsync)
    with pytest.raises(StoreWriteError, match="rolled back") as raised:
        pdcst.subscribe(podcast.feed.url, "{title}", None, parser, podcasts)
    assert failed == [True]
    assert not isinstance(raised.value, PublicationDurabilityError)
    assert "Published" not in str(raised.value)
    assert episodes.path.read_bytes() == episodes_before
    assert podcasts.path.read_bytes() == podcasts_before
    assert not list(episodes.base_dir.glob(".pdcst-refresh-*"))


def test_rollback_restores_original_episode_file_mode(
    tmp_path, monkeypatch, podcast, episode
):
    from pdcst.storage import os as storage_os

    podcasts, episodes, parser = _refresh_setup(tmp_path, monkeypatch, podcast, episode)
    episodes.path.chmod(0o600)
    before = episodes.path.read_bytes()
    replace = storage_os.replace

    def fail_subscription(source, target):
        if target == podcasts.path:
            raise OSError("subscription failed")
        return replace(source, target)

    previous_umask = os.umask(0o022)
    try:
        monkeypatch.setattr(storage_os, "replace", fail_subscription)
        with pytest.raises(OSError, match="subscription failed"):
            pdcst.subscribe(podcast.feed.url, "{title}", None, parser, podcasts)
    finally:
        os.umask(previous_umask)
    assert episodes.path.read_bytes() == before
    assert stat.S_IMODE(episodes.path.stat().st_mode) == 0o600
    assert not list(episodes.base_dir.glob(".pdcst-refresh-*"))


def test_backup_write_failure_leaves_no_recovery_file(
    tmp_path, monkeypatch, podcast, episode
):
    podcasts, episodes, parser = _refresh_setup(tmp_path, monkeypatch, podcast, episode)
    episodes_before = episodes.path.read_bytes()
    podcasts_before = podcasts.path.read_bytes()
    write = EpisodeRepository._write_refresh_backup

    def partial(self, backup, previous):
        write(self, backup, previous[:1])
        raise OSError("synthetic backup failure")

    monkeypatch.setattr(EpisodeRepository, "_write_refresh_backup", partial)
    with pytest.raises(OSError, match="synthetic backup failure"):
        pdcst.subscribe(podcast.feed.url, "{title}", None, parser, podcasts)
    assert episodes.path.read_bytes() == episodes_before
    assert podcasts.path.read_bytes() == podcasts_before
    assert not list(episodes.base_dir.glob(".pdcst-refresh-*"))


def test_stage_directory_sync_failure_is_an_ordinary_download_failure(
    tmp_path, monkeypatch, episode
):
    data = b"synthetic audio bytes"
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
            **kw
        ),
    )
    repo = EpisodeRepository(tmp_path)
    repo.add(episode)
    before = repo.path.read_bytes()
    sync = os.fsync

    def fsync(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError("synthetic directory sync")
        sync(fd)

    monkeypatch.setattr(os, "fsync", fsync)
    with pytest.raises(DownloadError, match="Unable to stage audio download") as raised:
        repo.download_episode(episode, "saved.mp3", download_with_progress)
    assert ".pdcst-download-" not in str(raised.value)
    assert repo.path.read_bytes() == before
    assert not (tmp_path / "saved.mp3").exists()
    assert not list(tmp_path.glob(".pdcst-download-*"))
