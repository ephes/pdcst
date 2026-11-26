"""Raising warning filters never replace publication outcomes or recovery paths."""

import copy
import os
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
