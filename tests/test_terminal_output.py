"""Feed-supplied text must not reach the terminal as control sequences."""

from copy import deepcopy

import httpx
import pytest
import typer
from typer.testing import CliRunner

import pdcst
from pdcst.repository import EpisodeRepository, PodcastRepository

# XML 1.0 admits CR/LF/TAB and C1 controls as character references. feedparser
# maps most C1 references to cp1252, but keeps U+0081/8D/8F/90/9D (RI, DCS,
# OSC, ...). Bidirectional overrides and line/paragraph separators pass as-is.
SHOW = "Show&#x9d;52;c;aGk=&#x202e;rewop"
SPOOF = "Second&#13;&#x8d;&#13;Fake line&#10;Injected&#x90;&#x2028;&#9;end"
FEED = (
    '<?xml version="1.0" encoding="utf-8"?><rss version="2.0"><channel>'
    f"<title>{SHOW}</title>"
    "<item><guid>one</guid><title>Real one</title>"
    "<pubDate>Fri, 01 Jul 2022 12:00:00 GMT</pubDate>"
    '<enclosure url="https://example.com/a.mp3" type="audio/mpeg"/></item>'
    f"<item><guid>two</guid><title>{SPOOF}</title>"
    "<pubDate>Sat, 02 Jul 2022 12:00:00 GMT</pubDate>"
    '<enclosure url="https://example.com/b.mp3" type="audio/mpeg"/></item>'
    "</channel></rss>"
).encode()

SHOW_TEXT = "Show\\u009d52;c;aGk=\\u202erewop"
SPOOF_TEXT = (
    "Second\\u000d\\u008d\\u000dFake line\\u000aInjected\\u0090\\u2028\\u0009end"
)


@pytest.fixture
def subscribed(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "feedparser.http.get", lambda *_args, **_kw: pytest.fail("unbounded fetch")
    )
    client = httpx.Client

    def handler(request):
        return httpx.Response(
            200,
            headers={"content-type": "application/rss+xml"},
            stream=httpx.ByteStream(FEED),
        )

    monkeypatch.setattr(
        "httpx.Client",
        lambda **kw: client(transport=httpx.MockTransport(handler), **kw),
    )
    monkeypatch.setattr(pdcst.settings, "root", tmp_path)
    result = CliRunner().invoke(
        pdcst.cli, ["add", "https://example.com/feed.xml"], color=False
    )
    assert result.exit_code == 0, result.output
    return result


# Written as escapes so the test source holds no invisible or reordering text.
BIDI_AND_SEPARATORS = (
    "\u061c\u200e\u200f\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069"
    "\u2028\u2029"
)


def assert_inert(output):
    controls = [
        char
        for char in output
        if char != "\n" and (ord(char) < 0x20 or 0x7F <= ord(char) <= 0x9F)
    ]
    assert controls == []
    assert not set(output) & set(BIDI_AND_SEPARATORS)


def test_add_escapes_feed_title(subscribed):
    assert_inert(subscribed.output)
    assert subscribed.output == f"Podcast {SHOW_TEXT} added\n"


def test_podcasts_escapes_stored_titles(subscribed):
    result = CliRunner().invoke(pdcst.cli, ["podcasts"], color=False)
    assert result.exit_code == 0, result.output
    assert_inert(result.output)
    assert result.output.splitlines() == [f"{SHOW_TEXT} - 2 episodes"]


def test_podcasts_bounds_shared_directory_diagnostic(tmp_path, monkeypatch, podcast):
    monkeypatch.setattr(pdcst.settings, "root", tmp_path)
    shows = []
    for index in range(12):
        show = deepcopy(podcast)
        show.title = f"Shared {index}\r\n" + "A" * 5000
        host = "a" * 5000 if index == 0 else "example.test:8443"
        show.feed.url = f"https://user:secret@{host}/private-{index}?token=x#fragment"
        shows.append(show)
    PodcastRepository(tmp_path).add_list(shows)

    with pytest.raises(typer.BadParameter) as raised:
        pdcst.list_podcasts()

    message = str(raised.value)
    assert len(message) < 1500
    assert message.splitlines() == [message]
    assert_inert(message)
    assert "distinct --podcast-dir" in message
    first_preview = "Shared 0\\u000d\\u000a" + "A" * 139 + "…"
    assert f"{first_preview} <feed origin unavailable>" in message
    for index in range(1, 5):
        preview = f"Shared {index}\\u000d\\u000a" + "A" * 139 + "…"
        assert f"{preview} <https://example.test:8443>" in message
    assert "Shared 5" not in message
    assert message.endswith("7 more conflicting feeds")
    for private in ("user:", "secret@", "/private-", "?token=", "#fragment"):
        assert private not in message


@pytest.mark.parametrize(
    "title,preview",
    [
        ("A" * 5000, "A" * 146 + "…"),
        ("A" + "\x9d" * 50, "A" + "\\u009d" * 24 + "…"),
        ("A" * 141 + "\x9d", "A" * 141 + "\\u009d"),
    ],
)
def test_podcasts_bounds_display_and_episodes_uses_exact_title(
    tmp_path, monkeypatch, podcast, episode, title, preview
):
    monkeypatch.setattr(pdcst.settings, "root", tmp_path)
    podcast.title = title
    archive = deepcopy(podcast)
    archive.title += " Archive"
    archive.directory = "archive"
    archive.feed.url = "https://example.test/archive"
    shows = PodcastRepository(tmp_path)
    shows.add_list([archive, podcast])
    EpisodeRepository(tmp_path / podcast.directory).add(episode)
    runner = CliRunner()

    result = runner.invoke(pdcst.cli, ["podcasts"])
    assert result.exit_code == 0, result.output
    assert_inert(result.output)
    archive_line, show_line = result.output.splitlines()
    assert show_line == f"{preview} - 3 episodes"
    assert len(archive_line) <= 160

    result = runner.invoke(pdcst.cli, ["episodes", title])
    assert result.exit_code == 0, result.output
    assert result.output == f"{episode.title}\n"


def test_episodes_escapes_stored_titles(subscribed):
    result = CliRunner().invoke(pdcst.cli, ["episodes", "show"], color=False)
    assert result.exit_code == 0, result.output
    assert_inert(result.output)
    # One line per episode: embedded CR/LF/RI cannot rewrite or add lines.
    assert result.output.splitlines() == ["Real one", SPOOF_TEXT]


def test_stored_titles_keep_original_text(subscribed):
    podcast = pdcst.list_all_podcasts()[0]
    assert "\u202e" in podcast.title
    titles = [episode.title for episode in pdcst.list_all_episodes(podcast)]
    assert "\r\x8d\rFake line\nInjected\x90" in titles[1]


@pytest.mark.parametrize(
    "text",
    [
        "Plain title",
        "Ümlaut café",
        # ZWJ (a format character) must survive inside emoji sequences.
        "Family \U0001f468\u200d\U0001f469\u200d\U0001f467 emoji",
        "日本語",
    ],
)
def test_terminal_text_keeps_printable_text(text):
    assert pdcst.terminal_text(text) == text


@pytest.mark.parametrize(
    "char",
    [chr(code) for code in (*range(0x20), *range(0x7F, 0xA0))]
    + list(BIDI_AND_SEPARATORS),
)
def test_terminal_text_escapes_unsafe_characters(char):
    assert pdcst.terminal_text(f"a{char}b") == f"a\\u{ord(char):04x}b"


def test_terminal_text_formats_objects(podcast, episode):
    podcast.title = "Show\x1b[2J"
    episode.title = "Episode\r\n"
    assert pdcst.terminal_text(podcast) == "Show\\u001b[2J - 3 episodes"
    assert pdcst.terminal_text(episode) == "Episode\\u000d\\u000a"
