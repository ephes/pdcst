"""Committed publications remain successful when intent retirement fails."""

import json
import os
from pathlib import Path

import pytest

import pdcst
from pdcst import repository
from pdcst.repository import (
    AUDIO_INTENT,
    REFRESH_INTENT,
    PodcastRepository,
    StoreReadError,
    clear_intent,
)


@pytest.mark.parametrize("admitted", [False, True])
def test_clear_intent_retries_parent_sync_after_unlink(tmp_path, monkeypatch, admitted):
    marker = tmp_path / AUDIO_INTENT
    marker.write_text("{}", encoding="utf-8")
    real_sync = repository.sync_directory
    attempts = []

    def sync_parent(path, *, directory_fd=None):
        assert path == tmp_path
        assert not marker.exists()
        attempts.append(directory_fd)
        if len(attempts) == 1:
            raise OSError("synthetic retirement sync failure")
        if directory_fd is not None:
            assert os.path.samestat(os.fstat(directory_fd), tmp_path.stat())
        real_sync(path, directory_fd=directory_fd)

    monkeypatch.setattr(repository, "sync_directory", sync_parent)
    descriptor = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY) if admitted else None
    try:
        with pytest.raises(OSError, match="synthetic retirement sync failure"):
            clear_intent(marker, directory_fd=descriptor)
        assert not marker.exists()

        clear_intent(marker, directory_fd=descriptor)
        assert len(attempts) == 2
        if admitted:
            assert attempts == [descriptor, descriptor]
    finally:
        if descriptor is not None:
            os.close(descriptor)


def test_clear_intent_leaves_missing_destination_tree_absent(tmp_path):
    parent = tmp_path / "missing" / "episodes"

    clear_intent(parent / AUDIO_INTENT)

    assert not parent.parent.exists()


@pytest.mark.parametrize("active_exception", [False, True])
@pytest.mark.parametrize("kind", ["audio", "refresh"])
@pytest.mark.parametrize("failure", ["unlink", "sync"])
def test_committed_publication_reports_pending_cleanup(
    publication_store, monkeypatch, kind, failure, active_exception
):
    home, episodes = publication_store
    monkeypatch.setattr(pdcst.settings, "root", home)
    shows = PodcastRepository(home)
    show = shows.list()[0]
    incoming = episodes.list()
    incoming[0].title = "Updated title"
    before = episodes.path.read_bytes()
    marker_name = AUDIO_INTENT if kind == "audio" else REFRESH_INTENT
    unlink = repository.os.unlink
    sync = repository.sync_directory
    retirement = []

    def intercept_unlink(path, *args, missing_ok=False, **kwargs):
        path = Path(path)
        if path.name == marker_name:
            retirement.append(episodes.base_dir / path)
            if failure == "unlink":
                raise OSError("PRIVATE_ERROR")
        try:
            return unlink(path, *args, **kwargs)
        except FileNotFoundError:
            if not missing_ok:
                raise

    def intercept_sync(path, **kwargs):
        if failure == "sync" and retirement:
            raise OSError("PRIVATE_ERROR")
        return sync(path, **kwargs)

    # Python 3.10's pathlib caches its unlink accessor; newer Python calls
    # os.unlink dynamically. Cover both without invoking the interceptor twice.
    monkeypatch.setattr(Path, "unlink", intercept_unlink)
    monkeypatch.setattr(repository.os, "unlink", intercept_unlink)
    monkeypatch.setattr(repository, "sync_directory", intercept_sync)

    class Parser:
        def get(self, *_):
            return show, incoming

    def publish():
        if kind == "audio":

            def transfer(_url, stage, **kwargs):
                stage.write_bytes(b"new audio")

            result = episodes.download_episode(incoming[0], "saved.mp3", transfer)
            assert result == episodes.base_dir / "saved.mp3"
        else:
            result = pdcst.subscribe(show.feed.url, "saved.mp3", None, Parser(), shows)
            assert result.episodes_count == 1

    with pytest.warns(
        RuntimeWarning, match="completed.*recovery cleanup pending"
    ) as warnings:
        if active_exception:
            try:
                raise RuntimeError("earlier handled caller error")
            except RuntimeError:
                publish()
        else:
            publish()
    message = str(warnings[0].message)
    assert "PRIVATE_ERROR" not in message
    assert str(episodes.base_dir / marker_name) in message
    assert "README interrupted-publication recovery" in message
    assert "repeat" in message
    assert retirement
    assert list(home.rglob("*.previous"))
    rows = json.loads(episodes.path.read_bytes())
    if kind == "audio":
        assert rows[0]["audio_file"] == "saved.mp3"
        assert (episodes.base_dir / "saved.mp3").read_bytes() == b"new audio"
    else:
        assert rows[0]["title"] == "Updated title"
        assert json.loads(shows.path.read_bytes())[0]["episodes_count"] == 1
        assert episodes.path.read_bytes() != before
    if failure == "unlink" or kind == "refresh":
        with pytest.raises(StoreReadError, match="unresolved publication"):
            (shows if kind == "refresh" else episodes).list()


def test_pending_cleanup_without_previous_audio_names_no_backup(
    publication_store, monkeypatch
):
    home, episodes = publication_store
    monkeypatch.setattr(pdcst.settings, "root", home)
    episode = episodes.list()[0]
    assert not (episodes.base_dir / "fresh.mp3").exists()
    unlink = repository.os.unlink

    def intercept_unlink(path, *args, missing_ok=False, **kwargs):
        path = Path(path)
        if path.name == AUDIO_INTENT:
            raise OSError("PRIVATE_ERROR")
        try:
            return unlink(path, *args, **kwargs)
        except FileNotFoundError:
            if not missing_ok:
                raise

    # Python 3.10's pathlib caches its unlink accessor; newer Python calls
    # os.unlink dynamically. Cover both without invoking the interceptor twice.
    monkeypatch.setattr(Path, "unlink", intercept_unlink)
    monkeypatch.setattr(repository.os, "unlink", intercept_unlink)

    def transfer(_url, stage, **kwargs):
        stage.write_bytes(b"new audio")

    with pytest.warns(RuntimeWarning, match="recovery cleanup pending") as warnings:
        result = episodes.download_episode(episode, "fresh.mp3", transfer)
    assert result == episodes.base_dir / "fresh.mp3"
    message = str(warnings[0].message)
    assert "previous audio" not in message
    assert "None" not in message
    assert str(episodes.base_dir / AUDIO_INTENT) in message
