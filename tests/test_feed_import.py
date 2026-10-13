import os
import time
from datetime import datetime, timezone

import feedparser
import pytest

from pdcst.repository import EpisodeRepository, FeedParserRepository, PodcastRepository

DATE = datetime(2022, 7, 1, 12, tzinfo=timezone.utc)


def item(
    enclosures='<enclosure url="https://example.com/a.mp3" type="audio/mpeg"/>',
    date="Fri, 01 Jul 2022 12:00:00 GMT",
    guid="one",
):
    return (
        f"<item><guid>{guid}</guid><title>Episode</title><pubDate>{date}</pubDate>"
        f"{enclosures}</item>"
    )


def fetch(monkeypatch, entries, channel_date=""):
    document = feedparser.parse(
        f'<rss version="2.0"><channel><title>Podcast</title>{channel_date}'
        f"{entries}</channel></rss>"
    )
    monkeypatch.setattr("pdcst.repository.feedparser.parse", lambda url: document)
    return FeedParserRepository().get("https://example.com/feed", None, "{index}.mp3")


def test_optional_channel_updated(monkeypatch):
    podcast, episodes = fetch(monkeypatch, item())
    assert podcast.feed.updated == episodes[0].published == DATE


def test_channel_updated_preferred(monkeypatch):
    podcast, _ = fetch(
        monkeypatch,
        item(),
        "<lastBuildDate>Sat, 02 Jul 2022 12:00:00 GMT</lastBuildDate>",
    )
    assert podcast.feed.updated == datetime(2022, 7, 2, 12, tzinfo=timezone.utc)


@pytest.mark.parametrize("entries", ["", "<item><title>Broken</title></item>"])
def test_unusable_feed_has_clear_error(monkeypatch, entries):
    with pytest.raises(ValueError, match="Feed has no usable audio episodes"):
        fetch(monkeypatch, entries)


def test_missing_feed_title(monkeypatch):
    document = feedparser.parse("<rss><channel/></rss>")
    monkeypatch.setattr("pdcst.repository.feedparser.parse", lambda url: document)
    with pytest.raises(ValueError, match="Feed is missing a title"):
        FeedParserRepository().get("https://example.com/feed", None, "{index}.mp3")


@pytest.mark.parametrize("zone", ["UTC", "Europe/Berlin"])
def test_dates_independent_of_host_timezone(monkeypatch, zone):
    original = os.environ.get("TZ")
    try:
        os.environ["TZ"] = zone
        time.tzset()
        _, episodes = fetch(monkeypatch, item())
        assert episodes[0].published == DATE
    finally:
        if original is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = original
        time.tzset()


def test_offset_order_and_storage(monkeypatch, tmp_path):
    podcast, episodes = fetch(
        monkeypatch,
        item(guid="later")
        + item(date="Fri, 01 Jul 2022 13:00:00 +0200", guid="earlier"),
    )
    assert [episode.guid for episode in episodes] == ["earlier", "later"]
    assert [episode.index for episode in episodes] == [1, 2]
    repo = EpisodeRepository(tmp_path)
    repo.add_list(episodes)
    assert [episode.published for episode in repo.list()] == [
        datetime(2022, 7, 1, 11, tzinfo=timezone.utc),
        DATE,
    ]
    podcasts = PodcastRepository(tmp_path)
    podcasts.add(podcast)
    assert podcasts.list()[0].feed.updated == DATE


def test_legacy_naive_storage_is_utc(tmp_path):
    (tmp_path / "episodes.json").write_text(
        '[{"index":1,"guid":"one","audio_url":"https://example.com/a.mp3",'
        '"published":"2022-07-01T12:00:00","title":"Episode","audio_file":null}]'
    )
    (tmp_path / "podcasts.json").write_text(
        '[{"feed":{"url":"https://example.com/feed","updated":"2022-07-01T12:00:00"},'
        '"title":"Podcast","episodes_count":1,"file_name_pattern":"{index}.mp3",'
        '"directory":"podcast"}]'
    )
    assert EpisodeRepository(tmp_path).list()[0].published == DATE
    assert PodcastRepository(tmp_path).list()[0].feed.updated == DATE


def test_audio_after_image(monkeypatch):
    _, episodes = fetch(
        monkeypatch,
        item(
            '<enclosure url="https://example.com/cover.jpg" type="image/jpeg"/>'
            '<enclosure url="https://example.com/audio" type="audio/mpeg"/>'
        ),
    )
    assert episodes[0].audio_url == "https://example.com/audio"


def test_untyped_audio_fallback(monkeypatch):
    _, episodes = fetch(
        monkeypatch, item('<enclosure url="https://example.com/a.mp3?download=1"/>')
    )
    assert episodes[0].audio_url == "https://example.com/a.mp3?download=1"


@pytest.mark.parametrize(
    "enclosures",
    [
        "",
        '<enclosure url="javascript:evil.mp3" type="audio/mpeg"/>',
        '<enclosure type="audio/mpeg"/>',
        '<enclosure url="https://example.com/file"/>',
        '<enclosure url="https://example.com/a.mp3" type="application/pdf"/>',
    ],
)
def test_invalid_entries_warn_and_preserve_siblings(monkeypatch, enclosures):
    with pytest.warns(UserWarning, match="Skipped 1 unusable feed entry"):
        podcast, episodes = fetch(monkeypatch, item(enclosures) + item(guid="valid"))
    assert podcast.episodes_count == 1
    assert episodes[0].guid == "valid"
    assert episodes[0].index == 1


def test_declared_audio_precedes_untyped_fallback(monkeypatch):
    _, episodes = fetch(
        monkeypatch,
        item(
            '<enclosure url="https://example.com/fallback.mp3"/>'
            '<enclosure url="https://example.com/preferred" type="audio/ogg"/>'
        ),
    )
    assert episodes[0].audio_url == "https://example.com/preferred"


def test_entry_date_conversion_without_channel_metadata():
    entries = feedparser.parse(f"<rss><channel>{item()}</channel></rss>").entries
    assert FeedParserRepository.parse_feed_entries(entries)[0]["published"] == DATE


def test_entry_audio_selection_without_channel_metadata():
    entries = feedparser.parse(
        "<rss><channel>"
        + item(
            '<enclosure url="https://example.com/cover.jpg" type="image/jpeg"/>'
            '<enclosure url="https://example.com/a.mp3" type="audio/mpeg"/>'
        )
        + "</channel></rss>"
    ).entries
    assert FeedParserRepository.parse_feed_entries(entries)[0]["audio_url"] == (
        "https://example.com/a.mp3"
    )


def test_invalid_channel_date_uses_newest_episode(monkeypatch):
    podcast, _ = fetch(monkeypatch, item(), "<lastBuildDate>unknown</lastBuildDate>")
    assert podcast.feed.updated == DATE


def test_invalid_episode_date_warns(monkeypatch):
    with pytest.warns(UserWarning, match="Skipped 1 unusable feed entry"):
        _, episodes = fetch(monkeypatch, item(date="unknown") + item(guid="valid"))
    assert [episode.guid for episode in episodes] == ["valid"]


def test_subscribe_without_channel_date(monkeypatch, tmp_path):
    import pdcst

    document = feedparser.parse(
        f"<rss><channel><title>Podcast</title>{item()}</channel></rss>"
    )
    monkeypatch.setattr("pdcst.repository.feedparser.parse", lambda url: document)
    monkeypatch.setattr(pdcst.settings, "root", tmp_path)
    podcast = pdcst.subscribe(
        "https://example.com/feed",
        "{index}.mp3",
        None,
        podcasts_repo=PodcastRepository(tmp_path),
    )
    assert podcast.feed.updated == DATE
    assert PodcastRepository(tmp_path).list()[0].episodes_count == 1
    assert EpisodeRepository(tmp_path / "podcast").list()[0].published == DATE


@pytest.mark.parametrize(
    "channel_date",
    [
        "<pubDate>Sat, 02 Jul 2022 12:00:00 GMT</pubDate>",
        "<lastBuildDate>unknown</lastBuildDate><pubDate>Sat, 02 Jul 2022 12:00:00 GMT</pubDate>",
    ],
)
def test_channel_publication_date_without_deprecated_alias(monkeypatch, channel_date):
    import warnings

    with warnings.catch_warnings():
        warnings.simplefilter("error", DeprecationWarning)
        podcast, _ = fetch(monkeypatch, item(), channel_date)
    assert podcast.feed.updated == datetime(2022, 7, 2, 12, tzinfo=timezone.utc)


@pytest.mark.parametrize(
    "channel_date, expected_day",
    [
        ("", 3),
        ("<lastBuildDate>unknown</lastBuildDate>", 3),
        ("<lastBuildDate>Sat, 02 Jul 2022 12:00:00 GMT</lastBuildDate>", 2),
    ],
)
def test_http_last_modified_fallback(monkeypatch, channel_date, expected_day):
    document = feedparser.parse(
        f"<rss><channel><title>Podcast</title>{channel_date}{item()}</channel></rss>"
    )
    document["updated_parsed"] = time.strptime(
        "2022-07-03 12:00:00", "%Y-%m-%d %H:%M:%S"
    )
    monkeypatch.setattr("pdcst.repository.feedparser.parse", lambda url: document)
    podcast, _ = FeedParserRepository().get(
        "https://example.com/feed", None, "{index}.mp3"
    )
    assert podcast.feed.updated == datetime(
        2022, 7, expected_day, 12, tzinfo=timezone.utc
    )


@pytest.mark.parametrize(
    "enclosure",
    [
        '<enclosure url="https://example.com/A.MP3?x=1"/>',
        '<enclosure url="https://example.com/A.MP3?x=1" type="Audio/MPEG; charset=utf-8"/>',
    ],
)
def test_audio_suffix_and_media_type_normalization(monkeypatch, enclosure):
    _, episodes = fetch(monkeypatch, item(enclosure))
    assert episodes[0].audio_url == "https://example.com/A.MP3?x=1"


@pytest.mark.parametrize(
    "href",
    [
        "https://example.com:invalid/a.mp3",
        "https:///a.mp3",
        "https://example.com/a b.mp3",
        " https://example.com/a.mp3 ",
    ],
)
def test_invalid_audio_urls_warn(monkeypatch, href):
    with pytest.warns(UserWarning, match="Skipped 2 unusable feed entry"):
        _, episodes = fetch(
            monkeypatch,
            item(f'<enclosure url="{href}" type="audio/mpeg"/>')
            + item("", guid="bad")
            + item(guid="valid"),
        )
    assert [episode.guid for episode in episodes] == ["valid"]
