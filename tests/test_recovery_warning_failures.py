"""Raising warning filters never replace publication outcomes or recovery paths."""

import copy
import os
import stat
import warnings
from pathlib import Path

import pytest

import pdcst
from pdcst import repository
from pdcst.repository import (
    REFRESH_INTENT,
    EpisodeRepository,
    PodcastRepository,
    StoreReadError,
)
from pdcst.storage import PublicationDurabilityError
from pdcst.storage import os as storage_os


def _notes(error):
    return "\n".join(getattr(error, "__notes__", ()))


def _refresh_store(tmp_path, monkeypatch, podcast, episode):
    monkeypatch.setattr(pdcst.settings, "root", tmp_path)
    podcasts = PodcastRepository(tmp_path)
    podcasts.add(podcast)
    episodes = EpisodeRepository(tmp_path / podcast.directory)
    episodes.add(episode)
    fresh = copy.deepcopy(episode)
    fresh.title = "Refreshed title"
    updated = copy.deepcopy(podcast)
    updated.episodes_count = 1

    class Parser:
        def get(self, *_):
            return updated, [fresh]

    return podcasts, episodes, Parser()


def test_refresh_rollback_failure_stays_primary_under_raising_warnings(
    tmp_path, monkeypatch, podcast, episode
):
    podcasts, episodes, parser = _refresh_store(tmp_path, monkeypatch, podcast, episode)
    before = episodes.path.read_bytes()
    replace = storage_os.replace
    publications = []
    initiating = OSError("subscription failed")

    def fail(source, target, **kwargs):
        if Path(target).name == podcasts.path.name:
            raise initiating
        if Path(target).name == episodes.path.name:
            publications.append(True)
            if len(publications) == 2:
                raise OSError("compensation failed")
        return replace(source, target, **kwargs)

    monkeypatch.setattr(storage_os, "replace", fail)
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        with pytest.raises(OSError) as raised:
            pdcst.subscribe(podcast.feed.url, "{title}", None, parser, podcasts)
    assert raised.value is initiating
    notes = _notes(raised.value)
    assert "Refresh rollback incomplete" in notes
    assert "compensation failed" not in notes
    (previous,) = episodes.base_dir.glob(".pdcst-refresh-*.previous")
    (refreshed,) = episodes.base_dir.glob(".pdcst-refresh-*.refreshed")
    assert previous.read_bytes() == before
    assert b"Refreshed title" in refreshed.read_bytes()
    for retained in (previous, refreshed, episodes.base_dir / REFRESH_INTENT):
        assert str(retained) in notes
    assert (episodes.base_dir / REFRESH_INTENT).exists()


def test_rolled_back_refresh_cleanup_failure_keeps_primary_under_raising_warnings(
    tmp_path, monkeypatch, podcast, episode
):
    podcasts, episodes, parser = _refresh_store(tmp_path, monkeypatch, podcast, episode)
    before = episodes.path.read_bytes()
    replace = storage_os.replace
    initiating = OSError("subscription failed")
    unlink = os.unlink

    def fail_subscription(source, target, **kwargs):
        if Path(target).name == podcasts.path.name:
            raise initiating
        return replace(source, target, **kwargs)

    def fail_marker(path, *args, **kwargs):
        if Path(path).name == REFRESH_INTENT:
            raise OSError("marker unlink failed")
        return unlink(path, *args, **kwargs)

    monkeypatch.setattr(storage_os, "replace", fail_subscription)
    monkeypatch.setattr(repository.os, "unlink", fail_marker)
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        with pytest.raises(OSError) as raised:
            pdcst.subscribe(podcast.feed.url, "{title}", None, parser, podcasts)
    assert raised.value is initiating
    notes = _notes(raised.value)
    assert "Refresh recovery cleanup incomplete" in notes
    assert str(episodes.base_dir / REFRESH_INTENT) in notes
    assert episodes.path.read_bytes() == before


def test_committed_refresh_marker_sync_failure_survives_raising_warnings(
    tmp_path, monkeypatch, podcast, episode
):
    podcasts, episodes, parser = _refresh_store(tmp_path, monkeypatch, podcast, episode)
    unlink = os.unlink
    sync = repository.sync_directory
    retirement = []

    def record_marker(path, *args, **kwargs):
        if Path(path).name == REFRESH_INTENT:
            retirement.append(path)
        return unlink(path, *args, **kwargs)

    def fail_retirement_sync(path, **kwargs):
        if retirement:
            raise OSError("marker sync failed")
        return sync(path, **kwargs)

    monkeypatch.setattr(repository.os, "unlink", record_marker)
    monkeypatch.setattr(repository, "sync_directory", fail_retirement_sync)
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        result = pdcst.subscribe(podcast.feed.url, "{title}", None, parser, podcasts)
    assert retirement
    assert result.episodes_count == 1
    assert b"Refreshed title" in episodes.path.read_bytes()
    # Recovery evidence stays on disk for the README recovery procedure.
    assert list(episodes.base_dir.glob(".pdcst-refresh-*.previous"))
    with pytest.raises(StoreReadError, match="unresolved publication"):
        podcasts.list()


def _download_store(tmp_path, episode):
    repo = EpisodeRepository(tmp_path)
    episode.audio_file = "saved.mp3"
    repo.add(episode)
    target = tmp_path / episode.audio_file
    target.write_bytes(b"previous audio")

    def transfer(url, stage, **kwargs):
        stage.write_bytes(b"completed audio")

    return repo, target, transfer


def test_audio_durability_error_survives_raising_warnings(
    tmp_path, monkeypatch, episode
):
    repo, target, transfer = _download_store(tmp_path, episode)
    # Whitespace makes the unchanged association's JSON publication observable.
    repo.path.write_bytes(b"\n" + repo.path.read_bytes())
    old_json = repo.path.read_bytes()
    sync = os.fsync

    def fsync(fd):
        if (
            stat.S_ISDIR(os.fstat(fd).st_mode)
            and target.read_bytes() == b"completed audio"
            and repo.path.read_bytes() != old_json
        ):
            raise OSError("synthetic metadata directory sync")
        sync(fd)

    monkeypatch.setattr(os, "fsync", fsync)
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        with pytest.raises(PublicationDurabilityError) as raised:
            repo.download_episode(episode, target.name, transfer)
    assert raised.value.path == repo.path
    (backup,) = tmp_path.glob(".pdcst-download-*.previous")
    assert backup.read_bytes() == b"previous audio"
    assert target.read_bytes() == b"completed audio"
    notes = _notes(raised.value)
    assert f"previous audio retained at {backup}" in notes
    assert "synthetic" not in notes


@pytest.mark.parametrize("rollback_step", ["completed", "previous"])
def test_audio_rollback_failure_stays_primary_under_raising_warnings(
    tmp_path, monkeypatch, episode, rollback_step
):
    repo, target, transfer = _download_store(tmp_path, episode)
    before = repo.path.read_bytes()
    replace = repository.os.replace
    initiating = TypeError("synthetic metadata failure")

    def fail_write(self, models):
        raise initiating

    def fail_rollback(source, destination, **kwargs):
        source, destination = Path(source), Path(destination)
        completed_move = (
            source.name == target.name
            and destination.name.startswith(".pdcst-download-")
            and not destination.name.endswith(".previous")
        )
        previous_move = (
            source.name.endswith(".previous") and destination.name == target.name
        )
        if (rollback_step == "completed" and completed_move) or (
            rollback_step == "previous" and previous_move
        ):
            raise OSError("private rollback failure")
        return replace(source, destination, **kwargs)

    monkeypatch.setattr(EpisodeRepository, "_write_models", fail_write)
    monkeypatch.setattr(repository.os, "replace", fail_rollback)
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        with pytest.raises(TypeError) as raised:
            repo.download_episode(episode, target.name, transfer)
    assert raised.value is initiating
    assert repo.path.read_bytes() == before
    (backup,) = tmp_path.glob("*.previous")
    assert backup.read_bytes() == b"previous audio"
    if rollback_step == "completed":
        completed = target
    else:
        (completed,) = (
            path
            for path in tmp_path.glob(".pdcst-download-*")
            if path != backup and not path.name.endswith("intent.json")
        )
        assert not target.exists()
    assert completed.read_bytes() == b"completed audio"
    notes = _notes(raised.value)
    assert "rollback incomplete" in notes
    assert str(completed) in notes
    assert str(backup) in notes
    assert "private rollback failure" not in notes


def test_rolled_back_audio_stage_survives_raising_warnings(
    tmp_path, monkeypatch, episode
):
    repo, target, transfer = _download_store(tmp_path, episode)
    before = repo.path.read_bytes()
    initiating = TypeError("synthetic metadata failure")

    def fail_write(self, models):
        raise initiating

    monkeypatch.setattr(EpisodeRepository, "_write_models", fail_write)
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        with pytest.raises(TypeError) as raised:
            repo.download_episode(episode, target.name, transfer)
    assert raised.value is initiating
    assert repo.path.read_bytes() == before
    assert target.read_bytes() == b"previous audio"
    (stage,) = (
        path
        for path in tmp_path.glob(".pdcst-download-*")
        if not path.name.endswith((".previous", "intent.json", ".lock"))
    )
    assert stage.read_bytes() == b"completed audio"
    assert f"completed audio retained at {stage}" in _notes(raised.value)
