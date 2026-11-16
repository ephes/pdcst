"""Subscription-wide filename preflight precedes metadata publication."""

from copy import deepcopy

import pytest

import pdcst
from pdcst.models import FilenamePatternError
from pdcst.repository import EpisodeRepository, PodcastRepository


@pytest.mark.parametrize(
    "pattern,titles",
    [
        ("constant.mp3", ("a", "b")),
        ("{title}.mp3", ("Straße", "STRASSE")),
        ("{title}.mp3", ("Same", "same")),
    ],
)
@pytest.mark.parametrize("retained", [False, True])
def test_colliding_plan_preserves_both_stores(
    tmp_path, monkeypatch, podcast, episode, pattern, titles, retained
):
    monkeypatch.setattr(pdcst.settings, "root", tmp_path)
    shows = PodcastRepository(tmp_path)
    episodes = EpisodeRepository(tmp_path / podcast.directory)
    first = deepcopy(episode)
    first.title = titles[0]
    first.audio_file = "saved.mp3"
    second = deepcopy(episode)
    second.guid = "second"
    second.title = titles[1]
    shows.add(podcast)
    episodes.add(first)
    (episodes.base_dir / "saved.mp3").write_bytes(b"owned audio")
    before = {p: p.read_bytes() for p in tmp_path.rglob("*.json")}

    class Parser:
        def get(self, *_):
            show = deepcopy(podcast)
            show.file_name_pattern = pattern
            return show, [second] if retained else [first, second]

    with pytest.raises(
        FilenamePatternError, match="collision.*distinct.*index"
    ) as error:
        pdcst.subscribe(podcast.feed.url, pattern, None, Parser(), shows)
    assert len(str(error.value)) < 200
    assert before == {p: p.read_bytes() for p in tmp_path.rglob("*.json")}
    assert not list(tmp_path.rglob(".pdcst-refresh-*"))
    assert (episodes.base_dir / "saved.mp3").read_bytes() == b"owned audio"


def test_unique_plan_retains_download_ownership(
    tmp_path, monkeypatch, podcast, episode
):
    monkeypatch.setattr(pdcst.settings, "root", tmp_path)
    podcast.file_name_pattern = "{index}.mp3"
    episodes = EpisodeRepository(tmp_path / podcast.directory)
    episode.audio_file = "1.mp3"
    episodes.add(episode)
    (episodes.base_dir / "1.mp3").write_bytes(b"owned audio")
    second = deepcopy(episode)
    second.guid = "second"

    class Parser:
        def get(self, *_):
            return podcast, [second, episode]

    for _ in range(2):
        pdcst.subscribe(
            podcast.feed.url, "{index}.mp3", None, Parser(), PodcastRepository(tmp_path)
        )
    rows = episodes.list()
    assert [r.index for r in rows] == [1, 2]
    assert [r.audio_file for r in rows] == ["1.mp3", None]
