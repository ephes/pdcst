"""Synthetic subscription root replacement barriers."""

from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest

import pdcst
from pdcst.repository import EpisodeRepository, PodcastRepository, StoreWriteError


@pytest.mark.parametrize("replacement", ["directory", "symlink"])
def test_subscription_root_replaced_during_acquisition(
    tmp_path, monkeypatch, podcast, episode, replacement
):
    root = tmp_path / "home"
    monkeypatch.setattr(pdcst.settings, "root", root)
    shows = PodcastRepository(root)
    shows.add(podcast)
    EpisodeRepository(root / podcast.directory).add(episode)
    before = {p.relative_to(root): p.read_bytes() for p in root.rglob("*.json")}
    admitted = tmp_path / "admitted"
    outside = tmp_path / "replacement"
    outside.mkdir()
    for path, data in before.items():
        (outside / path).parent.mkdir(parents=True, exist_ok=True)
        (outside / path).write_bytes(data)
    fetched, resume = Event(), Event()

    class Parser:
        def get(self, *_):
            fetched.set()
            assert resume.wait(5)
            podcast.title = "Changed synthetic title"
            return podcast, [episode]

    with ThreadPoolExecutor(max_workers=1) as pool:
        result = pool.submit(
            pdcst.subscribe, podcast.feed.url, "{index}.mp3", None, Parser(), shows
        )
        try:
            assert fetched.wait(5)
            root.rename(admitted)
            if replacement == "symlink":
                root.symlink_to(outside, target_is_directory=True)
            else:
                outside.rename(root)
                outside = root
            snapshot = {
                p.relative_to(outside): p.read_bytes()
                for p in outside.rglob("*")
                if p.is_file()
            }
        finally:
            resume.set()
        with pytest.raises(StoreWriteError, match="directory.*changed"):
            result.result(5)
    assert snapshot == {
        p.relative_to(outside): p.read_bytes()
        for p in outside.rglob("*")
        if p.is_file()
    }
    assert before == {
        p.relative_to(admitted): p.read_bytes() for p in admitted.rglob("*.json")
    }


@pytest.mark.parametrize("when", ["backup", "intent", "episodes"])
def test_subscription_root_replacement_keeps_rollback_in_admitted_tree(
    tmp_path, monkeypatch, podcast, episode, when
):
    from pdcst import repository

    root, moved = tmp_path / "home", tmp_path / "admitted"
    monkeypatch.setattr(pdcst.settings, "root", root)
    shows = PodcastRepository(root)
    shows.add(podcast)
    EpisodeRepository(root / podcast.directory).add(episode)
    before = {p.relative_to(root): p.read_bytes() for p in root.rglob("*.json")}
    swapped = []

    def swap():
        if swapped:
            return
        root.rename(moved)
        root.mkdir()
        for path, data in before.items():
            (root / path).parent.mkdir(parents=True, exist_ok=True)
            (root / path).write_bytes(data)
        swapped.append(True)

    backup = PodcastRepository._write_refresh_backup
    intent = repository.write_intent
    write = EpisodeRepository._write_models

    def at_backup(self, *args):
        if when == "backup":
            swap()
        return backup(self, *args)

    def at_intent(path, details, **kwargs):
        result = intent(path, details, **kwargs)
        if when == "intent":
            swap()
        return result

    def at_episodes(self, *args):
        result = write(self, *args)
        if when == "episodes":
            swap()
        return result

    class Parser:
        def get(self, *_):
            episode.title = "Changed synthetic episode"
            return podcast, [episode]

    monkeypatch.setattr(PodcastRepository, "_write_refresh_backup", at_backup)
    monkeypatch.setattr(repository, "write_intent", at_intent)
    monkeypatch.setattr(EpisodeRepository, "_write_models", at_episodes)
    with pytest.raises(StoreWriteError, match="directory.*changed"):
        pdcst.subscribe(podcast.feed.url, "{index}.mp3", None, Parser(), shows)
    assert swapped
    assert before == {
        p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()
    }
    assert before == {
        p.relative_to(moved): p.read_bytes() for p in moved.rglob("*.json")
    }
    assert not list(moved.rglob(".pdcst-refresh-*"))


@pytest.mark.parametrize("operation", ["read", "add"])
def test_direct_subscription_repository_uses_admitted_root(
    tmp_path, podcast, monkeypatch, operation
):
    root, moved = tmp_path / "home", tmp_path / "admitted"
    shows = PodcastRepository(root)
    shows.add(podcast)
    before = shows.path.read_bytes()
    swapped = []
    method = "_read_bytes" if operation == "read" else "_serialize_models"
    original = getattr(PodcastRepository, method)

    def intercept(self, *args):
        if not swapped:
            root.rename(moved)
            root.mkdir()
            (root / "podcasts.json").write_text("[]")
            swapped.append(True)
        return original(self, *args)

    monkeypatch.setattr(PodcastRepository, method, intercept)
    if operation == "read":
        assert shows.list()[0].title == podcast.title
    else:
        with pytest.raises(StoreWriteError, match="directory.*changed"):
            shows.add(podcast)
    assert (root / "podcasts.json").read_text() == "[]"
    assert (moved / "podcasts.json").read_bytes() == before
