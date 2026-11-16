"""Subscription paths cannot occupy the metadata/control namespaces."""

from copy import deepcopy

import pytest

import pdcst
from pdcst.repository import PodcastRepository


@pytest.mark.parametrize(
    "name",
    [
        ".pdcst-refresh-intent.json",
        ".PDCST-DOWNLOAD-INTENT.JSON",
        ".feed-locks",
        ".FEED-LOCKS/nested",
        "podcasts.json",
        "PODCASTS.JSON.LOCK",
        "episodes.json",
        "episodes.json.download.lock",
        "shows/.pdcst-refresh-backup.previous",
    ],
)
def test_explicit_control_directory_refused_before_mutation_or_fetch(
    tmp_path, monkeypatch, podcast, name
):
    monkeypatch.setattr(pdcst.settings, "root", tmp_path)
    repo = PodcastRepository(tmp_path)

    class Parser:
        def get(self, *_):
            pytest.fail("reserved directory reached feed acquisition")

    with pytest.raises(ValueError, match="directory.*reserved"):
        pdcst.subscribe(podcast.feed.url, "{index}.mp3", name, Parser(), repo)
    assert not list(tmp_path.iterdir())
    assert repo.list() == []


@pytest.mark.parametrize("directory", [".hidden", "custom/nested"])
def test_ordinary_custom_directories_still_work(
    tmp_path, monkeypatch, podcast, directory
):
    monkeypatch.setattr(pdcst.settings, "root", tmp_path)

    class Parser:
        def get(self, *_):
            show = deepcopy(podcast)
            show.directory = directory
            return show, []

    result = pdcst.subscribe(
        podcast.feed.url,
        "{index}.mp3",
        directory,
        Parser(),
        PodcastRepository(tmp_path),
    )
    assert result.directory == directory
    assert len(PodcastRepository(tmp_path).list()) == 1


def test_directory_alias_cannot_reach_control_namespace(tmp_path, monkeypatch, podcast):
    monkeypatch.setattr(pdcst.settings, "root", tmp_path)
    (tmp_path / ".feed-locks").mkdir()
    (tmp_path / "alias").symlink_to(tmp_path / ".feed-locks", target_is_directory=True)
    with pytest.raises(ValueError, match="directory.*reserved"):
        pdcst.subscribe(
            podcast.feed.url,
            "{index}.mp3",
            "alias",
            object(),
            PodcastRepository(tmp_path),
        )
    assert not list((tmp_path / ".feed-locks").iterdir())
