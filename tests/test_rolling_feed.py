"""Retained episode identity across rolling feed windows."""

from copy import deepcopy
from datetime import timedelta

import pdcst
from pdcst.repository import EpisodeRepository, PodcastRepository


def test_rolling_feed_keeps_indexes_counts_and_downloads(
    tmp_path, monkeypatch, podcast, episode
):
    monkeypatch.setattr(pdcst.settings, "root", tmp_path)
    podcast.file_name_pattern = "{index}.mp3"
    directory = tmp_path / podcast.directory

    def row(guid, day):
        result = deepcopy(episode)
        result.guid = guid
        result.title = guid
        result.published += timedelta(days=day)
        return result

    a, b, c, older = [
        row(guid, day) for guid, day in [("a", 0), ("b", 1), ("c", 2), ("older", -10)]
    ]

    def refresh(rows):
        # Feed indexes describe only the latest chronological window.
        rows = deepcopy(rows)
        for index, current in enumerate(sorted(rows, key=lambda r: r.published), 1):
            current.index = index

        class Parser:
            def get(self, *_):
                show = deepcopy(podcast)
                show.episodes_count = len(rows)
                return show, rows

        return pdcst.subscribe(
            podcast.feed.url,
            podcast.file_name_pattern,
            None,
            Parser(),
            PodcastRepository(tmp_path),
        )

    refresh([a, b])
    repo = EpisodeRepository(directory)
    saved = repo.list()[1]
    saved.audio_file = "2.mp3"
    (directory / saved.audio_file).write_bytes(b"saved audio")
    repo.add(saved)
    refreshed = refresh([c, b])
    assert refreshed.episodes_count == 3
    assert [(r.guid, r.index) for r in repo.list()] == [("a", 1), ("b", 2), ("c", 3)]
    refresh([older, c, b])
    refresh([b, older])
    reopened = EpisodeRepository(directory).list()
    assert [(r.guid, r.index) for r in reopened] == [
        ("a", 1),
        ("b", 2),
        ("c", 3),
        ("older", 4),
    ]
    assert reopened[1].audio_file == "2.mp3"
    assert (directory / "2.mp3").read_bytes() == b"saved audio"
    assert PodcastRepository(tmp_path).list()[0].episodes_count == 4
