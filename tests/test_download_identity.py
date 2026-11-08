"""Synthetic command-level ownership and feed/download interleavings."""

import copy
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path

import pytest
import typer

import pdcst
from pdcst.repository import EpisodeRepository


@pytest.fixture
def store(tmp_path, monkeypatch, podcast, episode):
    monkeypatch.setattr(pdcst.settings, "root", tmp_path)
    podcast.file_name_pattern = "{title}.mp3"
    episode.title = "same"
    episode.audio_url = "https://example.test/old.mp3"
    other = copy.deepcopy(episode)
    other.guid = "second-guid"
    repo = EpisodeRepository(tmp_path / podcast.directory)
    repo.add_list([episode, other])
    monkeypatch.setattr(pdcst, "find_podcast_by_identifier", lambda _, **kw: podcast)
    monkeypatch.setattr(
        pdcst,
        "find_episode_by_identifier",
        lambda _, key, **kw: copy.deepcopy(episode if key == "first" else other),
    )
    calls = []

    def transfer(url, target):
        calls.append(url)
        target.write_bytes(b"synthetic audio")

    monkeypatch.setattr(pdcst, "download_with_progress", transfer)
    return repo, episode, other, calls


def test_distinct_guids_cannot_overwrite(store):
    repo, episode, other, calls = store
    pdcst.download("show", "first")
    before = repo.path.read_bytes()
    with pytest.raises(typer.BadParameter, match="already owned"):
        pdcst.download("show", "second")
    assert len(calls) == 1
    assert repo.path.read_bytes() == before
    assert (repo.base_dir / "same.mp3").read_bytes() == b"synthetic audio"
    assert EpisodeRepository(repo.base_dir).all[other.guid].audio_file is None
    # Refreshing the owning GUID is deliberate and remains supported.
    pdcst.download("show", "first")
    assert len(calls) == 2


@pytest.mark.parametrize("name", ["same.mp3", "SAME.MP3"])
def test_unowned_file_is_preserved(store, name):
    repo, _, _, calls = store
    target = repo.base_dir / name
    target.write_bytes(b"unrelated bytes")
    before = repo.path.read_bytes()
    with pytest.raises(typer.BadParameter, match="unowned"):
        pdcst.download("show", "first")
    assert not calls
    assert target.read_bytes() == b"unrelated bytes"
    assert repo.path.read_bytes() == before


@pytest.mark.parametrize(
    "pattern", ["episodes.json", "episodes.json.lock", "episodes.json.download.lock"]
)
def test_repository_files_cannot_be_audio(store, pattern):
    repo, episode, _, calls = store
    episode.podcast.file_name_pattern = pattern
    before = repo.path.read_bytes()
    with pytest.raises(typer.BadParameter, match="reserved"):
        pdcst.download("show", "first")
    assert not calls
    assert repo.path.read_bytes() == before


def test_concurrent_claims_preserve_owner(store, monkeypatch):
    repo, episode, other, calls = store
    started = threading.Event()
    second_started = threading.Event()
    release = threading.Event()

    def transfer(url, target):
        calls.append(url)
        started.set()
        assert release.wait(5)
        target.write_bytes(b"first audio")

    monkeypatch.setattr(pdcst, "download_with_progress", transfer)

    def second():
        second_started.set()
        with pytest.raises(typer.BadParameter, match="already owned"):
            pdcst.download("show", "second")

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(pdcst.download, "show", "first")
        try:
            assert started.wait(5)
            later = pool.submit(second)
            assert second_started.wait(5)
        finally:
            release.set()
        first.result(timeout=10)
        later.result(timeout=10)
    assert len(calls) == 1
    saved = EpisodeRepository(repo.base_dir).all
    assert saved[episode.guid].audio_file == "same.mp3"
    assert saved[other.guid].audio_file is None
    assert (repo.base_dir / "same.mp3").read_bytes() == b"first audio"


@pytest.mark.parametrize("changed_enclosure", [False, True])
@pytest.mark.parametrize("existing_audio", [False, True])
def test_feed_refresh_during_download(
    store, monkeypatch, changed_enclosure, existing_audio
):
    repo, episode, _, _ = store
    target = repo.base_dir / "same.mp3"
    if existing_audio:
        episode.audio_file = target.name
        repo.add(episode)
        target.write_bytes(b"previous audio")
    fresh = copy.deepcopy(episode)
    fresh.title = "new title"
    fresh.index = 42
    fresh.published += timedelta(days=1)
    if changed_enclosure:
        fresh.audio_url = "https://example.test/new.mp3"
    refreshed = []

    def transfer(url, stage):
        assert url == episode.audio_url
        # A separate writer must finish while the download is in progress.
        with ThreadPoolExecutor(max_workers=1) as pool:
            pool.submit(EpisodeRepository(repo.base_dir).import_list, [fresh]).result(
                timeout=5
            )
        refreshed.append(True)
        stage.write_bytes(b"completed old enclosure")

    monkeypatch.setattr(pdcst, "download_with_progress", transfer)
    if changed_enclosure:
        with pytest.raises(
            typer.BadParameter, match="enclosure changed.*retained at"
        ) as error:
            pdcst.download("show", "first")
        recovery = str(error.value).split("retained at ", 1)[1]
        assert Path(recovery).read_bytes() == b"completed old enclosure"
        if existing_audio:
            assert target.read_bytes() == b"previous audio"
        else:
            assert not target.exists()
    else:
        pdcst.download("show", "first")
        assert target.read_bytes() == b"completed old enclosure"
        assert not list(repo.base_dir.glob(".pdcst-download-*"))
    assert refreshed == [True]
    saved = EpisodeRepository(repo.base_dir).all[episode.guid]
    assert saved.title == fresh.title
    assert saved.index == fresh.index
    assert saved.published == fresh.published
    assert saved.audio_url == fresh.audio_url
    assert saved.audio_file == (None if changed_enclosure else target.name)


def test_failed_transfer_releases_claim(store, monkeypatch):
    repo, _, _, calls = store

    def fail(url, stage):
        stage.write_bytes(b"partial")
        raise OSError("synthetic transfer failure")

    with monkeypatch.context() as patch:
        patch.setattr(pdcst, "download_with_progress", fail)
        with pytest.raises(OSError, match="synthetic"):
            pdcst.download("show", "first")
    assert not list(repo.base_dir.glob(".pdcst-download-*"))
    pdcst.download("show", "second")
    assert len(calls) == 1


@pytest.mark.parametrize("change", ["missing", "enclosure"])
def test_stale_selection_is_refused_before_transfer(store, change):
    repo, episode, _, calls = store
    current = repo._fetch_all()
    if change == "missing":
        del current[episode.guid]
    else:
        current[episode.guid].audio_url = "https://example.test/new.mp3"
    repo._write_models(current)
    before = repo.path.read_bytes()
    with pytest.raises(typer.BadParameter, match="enclosure changed"):
        pdcst.download("show", "first")
    assert not calls
    assert repo.path.read_bytes() == before


def test_ownership_is_rechecked_before_publication(store, monkeypatch):
    repo, _, other, _ = store
    target = repo.base_dir / "same.mp3"

    def transfer(url, stage):
        other.audio_file = target.name
        EpisodeRepository(repo.base_dir).add(other)
        target.write_bytes(b"other owner")
        stage.write_bytes(b"completed audio")

    monkeypatch.setattr(pdcst, "download_with_progress", transfer)
    with pytest.raises(typer.BadParameter, match="already owned.*retained at") as error:
        pdcst.download("show", "first")
    recovery = Path(str(error.value).split("retained at ", 1)[1])
    assert recovery.read_bytes() == b"completed audio"
    assert target.read_bytes() == b"other owner"
    assert EpisodeRepository(repo.base_dir).all[other.guid].audio_file == target.name


@pytest.mark.parametrize("kind", ["missing", "existing", "loop"])
def test_symlink_destination_refused(store, kind):
    repo, _, _, calls = store
    destination = repo.base_dir / ("same.mp3" if kind == "loop" else "other.mp3")
    if kind == "existing":
        destination.write_bytes(b"unrelated")
    (repo.base_dir / "same.mp3").symlink_to(destination)
    before = repo.path.read_bytes()
    with pytest.raises(typer.BadParameter, match="symlink"):
        pdcst.download("show", "first")
    assert not calls
    assert repo.path.read_bytes() == before
    if kind == "existing":
        assert destination.read_bytes() == b"unrelated"
    elif kind == "missing":
        assert not destination.exists()
    assert (repo.base_dir / "same.mp3").is_symlink()


def test_same_guid_case_change_does_not_adopt_existing_file(store):
    repo, episode, _, calls = store
    episode.audio_file = "SAME.MP3"
    repo.add(episode)
    (repo.base_dir / "SAME.MP3").write_bytes(b"previous audio")
    with pytest.raises(typer.BadParameter, match="unowned"):
        pdcst.download("show", "first")
    assert not calls
    assert (repo.base_dir / "SAME.MP3").read_bytes() == b"previous audio"


@pytest.mark.parametrize("when", ["before", "during"])
@pytest.mark.parametrize("kind", ["outside", "loop"])
def test_unsafe_sibling_association_does_not_block_download(
    store, monkeypatch, when, kind
):
    repo, _, other, calls = store
    outside = repo.base_dir.parent / "outside.mp3"
    outside.write_bytes(b"unrelated audio")
    link = repo.base_dir / "sibling-link.mp3"
    link.symlink_to(link.name if kind == "loop" else outside)
    other.audio_file = link.name
    transfer = pdcst.download_with_progress
    if when == "before":
        repo.add(other)
    else:

        def refresh(url, stage):
            EpisodeRepository(repo.base_dir).add(other)
            transfer(url, stage)

        monkeypatch.setattr(pdcst, "download_with_progress", refresh)
    pdcst.download("show", "first")
    assert len(calls) == 1
    assert outside.read_bytes() == b"unrelated audio"
    assert (repo.base_dir / "same.mp3").read_bytes() == b"synthetic audio"
    assert not list(repo.base_dir.glob(".pdcst-download-*"))


@pytest.mark.parametrize("operation", ["replace", "chmod"])
def test_completed_audio_has_recovery_diagnostic_on_filesystem_error(
    store, monkeypatch, operation
):
    repo, episode, _, _ = store
    target = repo.base_dir / "same.mp3"
    if operation == "chmod":
        target.write_bytes(b"previous audio")
        episode.audio_file = target.name
        repo.add(episode)
    injected = []

    def fail(*args, **kwargs):
        injected.append(True)
        raise OSError("synthetic publication failure")

    with monkeypatch.context() as patch:
        if operation == "replace":
            patch.setattr("pdcst.repository.os.replace", fail)
        else:
            patch.setattr(Path, "chmod", fail)
        with pytest.warns(RuntimeWarning, match="completed audio retained at"):
            with pytest.raises(OSError, match="synthetic publication"):
                pdcst.download("show", "first")
    assert injected == [True]
    (recovered,) = repo.base_dir.glob(".pdcst-download-*")
    assert recovered.read_bytes() == b"synthetic audio"
    if operation == "chmod":
        assert target.read_bytes() == b"previous audio"
    else:
        assert not target.exists()


def test_unicode_alias_unowned_file_is_preserved(store):
    repo, episode, _, calls = store
    episode.title = "café"
    target = repo.base_dir / "cafe\u0301.mp3"
    target.write_bytes(b"unrelated audio")
    before = repo.path.read_bytes()
    with pytest.raises(typer.BadParameter, match="unowned"):
        pdcst.download("show", "first")
    assert not calls
    assert target.read_bytes() == b"unrelated audio"
    assert repo.path.read_bytes() == before


@pytest.mark.parametrize("existing_audio", [False, True])
@pytest.mark.parametrize("failure", ["serialization", "replace", "interrupt"])
def test_metadata_failure_rolls_back_audio(store, monkeypatch, existing_audio, failure):
    from pdcst import repository

    repo, episode, _, calls = store
    target = repo.base_dir / "same.mp3"
    unrelated = repo.base_dir / "unrelated.mp3"
    unrelated.write_bytes(b"unrelated")
    if existing_audio:
        episode.audio_file = target.name
        repo.add(episode)
        target.write_bytes(b"previous audio")
    before = repo.path.read_bytes()
    injected = []

    def fail_write(self, models):
        injected.append(True)
        if failure == "interrupt":
            raise KeyboardInterrupt("synthetic interrupt")
        raise TypeError("synthetic serialization failure")

    original_replace = repository.os.replace

    def fail_replace(source, destination):
        if Path(destination) == repo.path:
            injected.append(True)
            raise OSError("synthetic metadata replace failure")
        return original_replace(source, destination)

    with monkeypatch.context() as patch:
        if failure in {"serialization", "interrupt"}:
            patch.setattr(EpisodeRepository, "_write_models", fail_write)
        else:
            patch.setattr(repository.os, "replace", fail_replace)
        with pytest.warns(RuntimeWarning, match="completed audio retained at"):
            with pytest.raises(
                (TypeError, OSError, KeyboardInterrupt), match="synthetic"
            ):
                pdcst.download("show", "first")
    assert injected == [True]
    assert repo.path.read_bytes() == before
    if existing_audio:
        assert target.read_bytes() == b"previous audio"
    else:
        assert not target.exists()
    stages = list(repo.base_dir.glob(".pdcst-download-*"))
    assert len(stages) == 1
    assert stages[0].read_bytes() == b"synthetic audio"
    assert unrelated.read_bytes() == b"unrelated"
    pdcst.download("show", "first")
    assert len(calls) == 2
    assert target.read_bytes() == b"synthetic audio"
    assert repo.all[episode.guid].audio_file == target.name
