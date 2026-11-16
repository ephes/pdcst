"""Finite directory-ownership work, without large real directory fixtures."""

from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from pdcst import repository
from pdcst.repository import DownloadConflict
from pdcst.storage import replacement_file


@pytest.fixture
def unowned_store(publication_store, episode):
    _, repo = publication_store
    (repo.base_dir / "saved.mp3").unlink()
    episode.audio_file = None
    repo.add(episode)
    return repo


def transfer(url, stage, *, directory_fd):
    with replacement_file(stage, binary=True, directory_fd=directory_fd) as output:
        output.write(b"completed audio")


@pytest.mark.parametrize("limit", ["entries", "time"])
def test_incomplete_scan_refuses_before_transfer_and_releases_locks(
    unowned_store, episode, monkeypatch, limit
):
    repo = unowned_store
    before = repo.path.read_bytes()
    consumed = []
    closed = []
    calls = []
    clock = [0.0]

    @contextmanager
    def entries(self):
        def rows():
            for index in range(
                40
            ):  # models a growing iterator; finite even without the fix
                consumed.append(index)
                clock[0] += 0.25
                yield SimpleNamespace(name=f"unrelated-{index}")

        try:
            yield rows()
        finally:
            closed.append(True)

    def unexpected_transfer(*args, **kwargs):
        calls.append(True)
        pytest.fail("An incomplete scan must not reach transfer")

    with monkeypatch.context() as patch:
        patch.setattr(
            repository, "AUDIO_DESTINATION_SCAN_MAX_ENTRIES", 4, raising=False
        )
        patch.setattr(
            repository,
            "AUDIO_DESTINATION_SCAN_MAX_SECONDS",
            0.5 if limit == "time" else 100,
            raising=False,
        )
        patch.setattr(repository.time, "monotonic", lambda: clock[0])
        patch.setattr(repository.EpisodeRepository, "_directory_entries", entries)
        with pytest.raises(
            DownloadConflict, match="ownership scan incomplete.*narrow.*retry"
        ):
            repo.download_episode(episode, "saved.mp3", unexpected_transfer)
    assert len(consumed) == (2 if limit == "time" else 4)
    assert closed == [True]
    assert not calls
    assert repo.path.read_bytes() == before
    assert not (repo.base_dir / "saved.mp3").exists()
    assert not list(repo.base_dir.glob(".pdcst-download-*"))
    # Both metadata and download claims have been released after refusal.
    repo.download_episode(episode, "saved.mp3", transfer)
    assert repo.all[episode.guid].audio_file == "saved.mp3"
    assert (repo.base_dir / "saved.mp3").read_bytes() == b"completed audio"


@pytest.mark.parametrize("limit", ["entries", "time"])
def test_admission_and_publication_share_scan_budget(
    unowned_store, episode, monkeypatch, limit
):
    repo = unowned_store
    before = repo.path.read_bytes()
    consumed = []
    calls = []
    clock = [0.0]

    @contextmanager
    def entries(self):
        def rows():
            for index in range(3 if limit == "entries" else 2):
                consumed.append(index)
                clock[0] += 0.25
                yield SimpleNamespace(name=f"unrelated-{index}")

        yield rows()

    def slow_transfer(url, stage, **kwargs):
        calls.append(True)
        clock[0] += 100  # transfer/lock waiting is outside the active scan deadline
        transfer(url, stage, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(
            repository, "AUDIO_DESTINATION_SCAN_MAX_ENTRIES", 5, raising=False
        )
        patch.setattr(
            repository,
            "AUDIO_DESTINATION_SCAN_MAX_SECONDS",
            0.75 if limit == "time" else 100,
            raising=False,
        )
        patch.setattr(repository.time, "monotonic", lambda: clock[0])
        patch.setattr(repository.EpisodeRepository, "_directory_entries", entries)
        with pytest.raises(
            DownloadConflict, match="ownership scan incomplete.*retained at"
        ):
            repo.download_episode(episode, "saved.mp3", slow_transfer)
    assert len(consumed) == (3 if limit == "time" else 5)
    assert calls == [True]
    assert repo.path.read_bytes() == before
    assert not (repo.base_dir / "saved.mp3").exists()
    (stage,) = repo.base_dir.glob(".pdcst-download-*")
    assert stage.read_bytes() == b"completed audio"
    repo.download_episode(episode, "saved.mp3", transfer)
    assert repo.all[episode.guid].audio_file == "saved.mp3"
    assert stage.read_bytes() == b"completed audio"


def test_alias_match_at_last_allowed_entry_is_still_unowned(
    unowned_store, episode, monkeypatch
):
    @contextmanager
    def entries(self):
        def rows():
            yield SimpleNamespace(name="unrelated")
            yield SimpleNamespace(name="CAFE\u0301.MP3")
            pytest.fail("A known collision must stop scanning immediately")

        yield rows()

    monkeypatch.setattr(repository, "AUDIO_DESTINATION_SCAN_MAX_ENTRIES", 2)
    monkeypatch.setattr(repository.EpisodeRepository, "_directory_entries", entries)
    with pytest.raises(DownloadConflict, match="unowned existing file"):
        unowned_store.download_episode(episode, "café.mp3", transfer)
    assert unowned_store.all[episode.guid].audio_file is None


def test_owned_destination_does_not_scan_unrelated_entries(
    publication_store, episode, monkeypatch
):
    _, repo = publication_store

    def entries(self):
        pytest.fail("An owned regular destination needs no directory scan")

    monkeypatch.setattr(repository.EpisodeRepository, "_directory_entries", entries)
    repo.download_episode(episode, "saved.mp3", transfer)
    assert (repo.base_dir / "saved.mp3").read_bytes() == b"completed audio"
