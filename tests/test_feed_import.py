import os
import time
import warnings
from datetime import datetime, timezone

import feedparser
import httpx
import pytest
from typer.testing import CliRunner

import pdcst
from pdcst.repository import (
    EpisodeRepository,
    FeedError,
    FeedParserRepository,
    PodcastRepository,
)

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


def mock_feed(monkeypatch, entries, channel_date=""):
    document = feedparser.parse(
        f'<rss version="2.0"><channel><title>Podcast</title>{channel_date}'
        f"{entries}</channel></rss>"
    )
    monkeypatch.setattr(
        FeedParserRepository, "fetch_document", staticmethod(lambda url: document)
    )
    return document


def fetch(monkeypatch, entries, channel_date=""):
    mock_feed(monkeypatch, entries, channel_date)
    return FeedParserRepository().get("https://example.com/feed", None, "{index}.mp3")


def test_optional_channel_updated(monkeypatch):
    podcast, episodes = fetch(monkeypatch, item())
    assert podcast.feed.updated == episodes[0].published == DATE


def test_download_duplicate_titles_by_exact_guid(monkeypatch, tmp_path):
    monkeypatch.setattr(pdcst.settings, "root", tmp_path)
    chosen_url = "https://example.com/chosen.mp3"
    fetch(
        monkeypatch,
        item(guid="episode-1")
        + item(f'<enclosure url="{chosen_url}" type="audio/mpeg"/>', guid="episode-2"),
    )
    podcast = pdcst.subscribe("https://example.com/feed", "{index}.{file_format}", None)
    calls = []

    def transfer(url, stage, **kwargs):
        calls.append(url)
        stage.write_bytes(b"synthetic audio")

    monkeypatch.setattr(pdcst, "download_with_progress", transfer)
    runner = CliRunner()
    result = runner.invoke(pdcst.cli, ["episodes", "Podcast"])
    assert result.exit_code == 0, result.output
    assert result.output.splitlines() == ["Episode", "Episode"]

    result = runner.invoke(pdcst.cli, ["download", "Podcast", "Episode"])
    assert result.exit_code == 2
    assert "Ambiguous episode (2 matches)" in result.output
    assert not calls

    result = runner.invoke(pdcst.cli, ["episodes", "Podcast", "--show-guid"])
    assert result.exit_code == 0, result.output
    assert result.output.splitlines() == ["Episode\tepisode-1", "Episode\tepisode-2"]

    for args, message in [
        (["--guid", "EPISODE-2"], "No matching episode"),
        (["--guid", "episode-"], "No matching episode"),
        (["Episode", "--guid", "episode-2"], "Supply an episode title or --guid"),
        ([], "Supply an episode title or --guid"),
    ]:
        result = runner.invoke(pdcst.cli, ["download", "Podcast", *args])
        assert result.exit_code == 2
        assert message in result.output
        assert not calls

    result = runner.invoke(pdcst.cli, ["download", "Podcast", "--guid", "episode-2"])
    assert result.exit_code == 0, result.output
    assert calls == [chosen_url]
    directory = tmp_path / podcast.directory
    first, second = EpisodeRepository(directory).list()
    assert first.audio_file is None
    assert (directory / second.audio_file).read_bytes() == b"synthetic audio"


def test_channel_updated_preferred(monkeypatch):
    podcast, _ = fetch(
        monkeypatch,
        item(),
        "<lastBuildDate>Sat, 02 Jul 2022 12:00:00 GMT</lastBuildDate>",
    )
    assert podcast.feed.updated == datetime(2022, 7, 2, 12, tzinfo=timezone.utc)


def test_channel_updated_cannot_predate_newest_admitted_episode(monkeypatch):
    podcast, episodes = fetch(
        monkeypatch,
        item() + item(date="Sat, 02 Jul 2022 12:00:00 GMT", guid="newest"),
        "<lastBuildDate>Thu, 30 Jun 2022 12:00:00 GMT</lastBuildDate>",
    )
    assert podcast.feed.updated == episodes[-1].published


@pytest.mark.parametrize("entries", ["", "<item><title>Broken</title></item>"])
def test_unusable_feed_has_clear_error(monkeypatch, entries):
    with pytest.raises(ValueError, match="Feed has no usable audio episodes"):
        fetch(monkeypatch, entries)


def test_missing_feed_title(monkeypatch):
    document = feedparser.parse("<rss><channel/></rss>")
    monkeypatch.setattr(
        FeedParserRepository, "fetch_document", staticmethod(lambda url: document)
    )
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
    ("media_type", "suffix", "rejected_paths"),
    [
        ("application/octet-stream", "mp3", ("file.bin?name=a.mp3",)),
        ("binary/octet-stream", "mp3", ("file.bin?name=a.mp3",)),
        ("application/ogg", "oga", ("video.ogv", "download?name=a.oga", "audio.mp3")),
        ("Application/Ogg; param=value", "ogg", ("video.ogx",)),
    ],
)
def test_suffix_based_enclosures_import_and_download(
    monkeypatch, tmp_path, media_type, suffix, rejected_paths
):
    monkeypatch.setattr(pdcst.settings, "root", tmp_path)
    feed_url = "https://example.com/feed"
    audio_url = f"https://example.com/A.{suffix.upper()}?download=1"
    generic = f'<enclosure url="{audio_url}" type="{media_type}"/>'
    typed_url = "https://example.com/preferred"
    generic_guid = "https://example.com/generic"
    entries = (
        item(generic, guid=generic_guid)
        + item(
            generic + f'<enclosure url="{typed_url}" type="audio/ogg"/>',
            guid="preferred",
        )
        + "".join(
            item(
                f'<enclosure url="https://example.com/{path}" type="{media_type}"/>',
                guid=f"non-audio-{index}",
            )
            for index, path in enumerate(rejected_paths)
        )
        + item(
            '<enclosure url="https://example.com/image.mp3" type="image/jpeg"/>',
            guid="image",
        )
        + item(
            '<enclosure url="https://example.com/video.mp3" type="video/mp4"/>',
            guid="video",
        )
    )

    def respond(request):
        if str(request.url) == feed_url:
            body = f"<rss><channel><title>Podcast</title>{entries}</channel></rss>"
            return httpx.Response(
                200,
                headers={"Content-Type": "application/rss+xml"},
                stream=httpx.ByteStream(body.encode()),
            )
        assert str(request.url) == audio_url
        return httpx.Response(
            200,
            headers={"Content-Type": media_type},
            stream=httpx.ByteStream(b"synthetic audio"),
        )

    client = httpx.Client
    monkeypatch.setattr(
        httpx,
        "Client",
        lambda **kw: client(transport=httpx.MockTransport(respond), **kw),
    )

    with pytest.warns(
        UserWarning, match=f"Skipped {len(rejected_paths) + 2} unusable feed entry"
    ):
        podcast = pdcst.subscribe(feed_url, "{index}.{file_format}", None)
    directory = tmp_path / podcast.directory
    repository = EpisodeRepository(directory)
    fallback, preferred = repository.list()
    assert (fallback.guid, fallback.audio_url, fallback.audio_media_type) == (
        generic_guid,
        audio_url,
        None,
    )
    assert (preferred.guid, preferred.audio_url, preferred.audio_media_type) == (
        typed_url,
        typed_url,
        "audio/ogg",
    )

    pdcst.download("Podcast", guid=generic_guid)
    filename = f"1.{suffix}"
    assert repository.list()[0].audio_file == filename
    assert (directory / filename).read_bytes() == b"synthetic audio"


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


@pytest.mark.parametrize("warning_action", ["default", "error"])
def test_refresh_preserves_usable_entries_under_warning_filters(
    monkeypatch, tmp_path, warning_action
):
    monkeypatch.setattr(pdcst.settings, "root", tmp_path)
    feed_url = "https://example.com/feed"
    name_pattern = "{index}.{file_format}"
    valid = item(guid="stored")
    broken = item(enclosures="", guid="unusable")
    mock_feed(monkeypatch, valid)
    pdcst.subscribe(feed_url, name_pattern, None)

    mock_feed(monkeypatch, item(guid="fresh") + broken + valid)
    with warnings.catch_warnings(record=True) as recorded:
        warnings.simplefilter(warning_action)
        refreshed = pdcst.subscribe(feed_url, name_pattern, None)
    expected = [(UserWarning, "Skipped 1 unusable feed entry/entries")]
    assert [(warn.category, str(warn.message)) for warn in recorded] == (
        expected if warning_action == "default" else []
    )
    assert refreshed.episodes_count == 2
    repo = EpisodeRepository(tmp_path / refreshed.directory)
    assert [episode.guid for episode in repo.list()] == ["stored", "fresh"]

    paths = [tmp_path / "podcasts.json", repo.base_dir / "episodes.json"]
    snapshots = [path.read_bytes() for path in paths]
    mock_feed(monkeypatch, broken)
    with warnings.catch_warnings():
        warnings.simplefilter(warning_action)
        with pytest.raises(FeedError, match="Feed has no usable audio episodes"):
            pdcst.subscribe(feed_url, name_pattern, None)
    assert [path.read_bytes() for path in paths] == snapshots


def test_feed_preview_keeps_usable_entries_when_warnings_are_errors(monkeypatch):
    document = mock_feed(monkeypatch, item(enclosures="", guid="unusable") + item())
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        preview = FeedParserRepository.parse_feed_entries(document.entries)
    assert [(episode["guid"], episode["index"]) for episode in preview] == [("one", 1)]


@pytest.mark.parametrize("include_valid_sibling", [True, False])
def test_model_rejection_isolated_before_duplicate_check(
    monkeypatch, include_valid_sibling
):
    # Simulate a candidate that passes enclosure selection but fails EpisodeInDb.
    media_type = "audio/x-synthetic"
    monkeypatch.setattr(
        FeedParserRepository,
        "AUDIO_TYPES",
        FeedParserRepository.AUDIO_TYPES | {media_type},
    )
    entries = item(
        f'<enclosure url="https://example.test/invalid" type="{media_type}"/>'
    )
    if include_valid_sibling:
        with pytest.warns(
            UserWarning, match="^Skipped 1 unusable feed entry/entries$"
        ) as warnings:
            podcast, episodes = fetch(monkeypatch, entries + item())
        assert len(warnings) == 1
        assert podcast.episodes_count == 1
        assert [
            (episode.guid, episode.index, episode.audio_media_type)
            for episode in episodes
        ] == [("one", 1, "audio/mpeg")]
    else:
        with pytest.raises(FeedError, match="Feed has no usable audio episodes"):
            fetch(monkeypatch, entries)


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


@pytest.mark.parametrize("guids", [("b", "a"), ("a", "b")])
def test_preview_indexes_match_fresh_import_for_equal_dates(
    monkeypatch, tmp_path, guids
):
    items = "".join(item(guid=guid) for guid in guids)
    document = feedparser.parse(
        f"<rss><channel><title>Podcast</title>{items}</channel></rss>"
    )
    monkeypatch.setattr(
        FeedParserRepository, "fetch_document", staticmethod(lambda url: document)
    )
    preview = FeedParserRepository.parse_feed_entries(document.entries)
    podcast = pdcst.subscribe(
        "https://example.com/feed",
        "{index}.mp3",
        None,
        podcasts_repo=PodcastRepository(tmp_path),
    )
    stored = EpisodeRepository(tmp_path / podcast.directory).list()
    assert [(episode.guid, episode.index) for episode in stored] == [("a", 1), ("b", 2)]
    assert [(episode["guid"], episode["index"]) for episode in preview] == [
        ("a", 1),
        ("b", 2),
    ]


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


def test_atom_entry_dates_on_import_and_refresh(monkeypatch, tmp_path):
    def atom_entry(guid, timestamps):
        return (
            f"<entry><id>{guid}</id><title>Episode</title>{timestamps}"
            '<link rel="enclosure" href="https://example.com/a.mp3" type="audio/mpeg"/>'
            "</entry>"
        )

    def refresh(updated_day):
        entries = (
            atom_entry(
                "updated", f"<updated>2022-07-{updated_day:02}T12:00:00Z</updated>"
            )
            + atom_entry(
                "published",
                "<published>2022-07-01T12:00:00Z</published>"
                "<updated>2022-07-04T12:00:00Z</updated>",
            )
            + atom_entry(
                "fallback",
                "<published>unknown</published><updated>2022-07-03T12:00:00Z</updated>",
            )
            + atom_entry("undated", "")
            + atom_entry("invalid", "<updated>unknown</updated>")
        )
        document = feedparser.parse(
            '<feed xmlns="http://www.w3.org/2005/Atom"><title>Podcast</title>'
            f"{entries}</feed>"
        )
        monkeypatch.setattr(
            FeedParserRepository, "fetch_document", staticmethod(lambda url: document)
        )
        with pytest.warns(UserWarning) as skipped:
            podcast = pdcst.subscribe(
                "https://example.com/feed",
                "{index}.mp3",
                None,
                podcasts_repo=PodcastRepository(tmp_path),
            )
        stored = EpisodeRepository(tmp_path / podcast.directory).list()
        assert [(row.guid, row.index, row.published) for row in stored] == [
            ("published", 1, DATE),
            ("updated", 2, DATE.replace(day=updated_day)),
            ("fallback", 3, DATE.replace(day=3)),
        ]
        assert podcast.episodes_count == 3
        assert podcast.feed.updated == DATE.replace(day=max(3, updated_day))
        assert [str(warning.message) for warning in skipped] == [
            "Skipped 2 unusable feed entry/entries"
        ]

    refresh(2)
    refresh(5)


def test_subscribe_without_channel_date(monkeypatch, tmp_path):
    document = feedparser.parse(
        f"<rss><channel><title>Podcast</title>{item()}</channel></rss>"
    )
    monkeypatch.setattr(
        FeedParserRepository, "fetch_document", staticmethod(lambda url: document)
    )
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
    "channel_date, modified, expected_day",
    [
        ("", "2022-06-30", 1),
        ("", "2022-07-03", 3),
        ("<lastBuildDate>unknown</lastBuildDate>", "2022-07-03", 3),
        (
            "<lastBuildDate>Sat, 02 Jul 2022 12:00:00 GMT</lastBuildDate>",
            "2022-07-03",
            2,
        ),
    ],
)
def test_http_last_modified_fallback(monkeypatch, channel_date, modified, expected_day):
    document = feedparser.parse(
        f"<rss><channel><title>Podcast</title>{channel_date}{item()}</channel></rss>"
    )
    document["updated_parsed"] = time.strptime(
        f"{modified} 12:00:00", "%Y-%m-%d %H:%M:%S"
    )
    monkeypatch.setattr(
        FeedParserRepository, "fetch_document", staticmethod(lambda url: document)
    )
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
