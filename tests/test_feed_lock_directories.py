"""Feed locks retain no-follow directory authority through publication."""

from contextlib import contextmanager

import pytest

import pdcst
from pdcst import repository
from pdcst.repository import EpisodeRepository, PodcastRepository, StoreWriteError


@pytest.mark.parametrize("inside", [False, True])
def test_symlinked_feed_lock_directory_refused_before_fetch(
    tmp_path, monkeypatch, inside
):
    root = tmp_path / "home"
    root.mkdir()
    target = root / "alias" if inside else tmp_path / "outside"
    target.mkdir()
    (root / ".feed-locks").symlink_to(target, target_is_directory=True)
    monkeypatch.setattr(pdcst.settings, "root", root)

    class Parser:
        def get(self, *_):
            pytest.fail("symlinked lock directory must be refused before fetch")

    with pytest.raises(StoreWriteError, match="directory"):
        pdcst.subscribe("https://example.test/feed", "{index}.mp3", None, Parser())
    assert not list(target.iterdir())
    assert not list(root.rglob("*.json"))


@pytest.mark.parametrize("when", ["acquire", "held", "fetch"])
@pytest.mark.parametrize("replacement", ["directory", "symlink"])
def test_replaced_feed_lock_directory_refused_without_redirecting_lock(
    tmp_path, monkeypatch, podcast, episode, when, replacement
):
    root = tmp_path / "home"
    monkeypatch.setattr(pdcst.settings, "root", root)
    shows = PodcastRepository(root)
    shows.add(podcast)
    episodes = EpisodeRepository(root / podcast.directory)
    episodes.add(episode)
    before = {p: p.read_bytes() for p in root.rglob("*.json")}
    directory = root / ".feed-locks"
    directory.mkdir()
    moved = root / "admitted-locks"
    outside = tmp_path / "outside"
    outside.mkdir()
    swapped, fetched = [], []

    def swap():
        directory.rename(moved)
        if replacement == "directory":
            directory.mkdir()
        else:
            directory.symlink_to(outside, target_is_directory=True)
        swapped.append(True)

    lock = repository.advisory_lock

    @contextmanager
    def intercept(path, **kwargs):
        if path.parent.name == ".feed-locks" and when == "acquire":
            swap()
        with lock(path, **kwargs):
            if path.parent.name == ".feed-locks" and when == "held":
                swap()
            yield

    class Parser:
        def get(self, *_):
            fetched.append(True)
            if when == "fetch":
                swap()
            episode.title = "Changed synthetic title"
            return podcast, [episode]

    monkeypatch.setattr(repository, "advisory_lock", intercept)
    with pytest.raises(StoreWriteError, match="directory"):
        pdcst.subscribe(podcast.feed.url, "{index}.mp3", None, Parser(), shows)
    assert swapped
    assert bool(fetched) == (when == "fetch")
    assert not list(directory.iterdir())
    assert not list(outside.iterdir())
    assert before == {p: p.read_bytes() for p in root.rglob("*.json")}
