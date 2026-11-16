import traceback

import feedparser
import httpx
import pytest

import pdcst
from pdcst.repository import (
    EpisodeRepository,
    FeedError,
    FeedParserRepository,
    PodcastRepository,
)
from tests.test_feed_import import item

ENCLOSURE = '<enclosure url="audio.mp3" type="audio/mpeg"/>'
BODY = (
    "<rss><channel><title>Synthetic</title>"
    + item(enclosures=ENCLOSURE)
    + "</channel></rss>"
).encode()


@pytest.fixture
def transport(monkeypatch):
    # No test may fall back to feedparser's networking implementation.
    monkeypatch.setattr(
        "feedparser.http.get", lambda *_args, **_kw: pytest.fail("unbounded fetch")
    )
    client = httpx.Client

    def install(handler):
        monkeypatch.setattr(
            "httpx.Client",
            lambda **kw: client(transport=httpx.MockTransport(handler), **kw),
        )

    return install


@pytest.mark.parametrize(
    "case",
    ["length", "chunks", "slow", "headers", "loop", "status", "broken", "encoding"],
)
def test_fetch_failures_preserve_stores(
    tmp_path, monkeypatch, transport, podcast, episode, case
):
    monkeypatch.setattr(pdcst.settings, "root", tmp_path)
    podcasts = PodcastRepository(tmp_path)
    podcasts.add(podcast)
    EpisodeRepository(tmp_path / podcast.directory).add(episode)
    before = {p: p.read_bytes() for p in tmp_path.rglob("*.json")}
    clock = [0.0]
    monkeypatch.setattr("time.monotonic", lambda: clock[0])
    closed = []
    requests = []
    consumed = []

    class Stream(httpx.SyncByteStream):
        def __iter__(self):
            if case == "broken":
                raise httpx.ReadError("synthetic failure")
            for _ in range(3):
                consumed.append(True)
                if case == "slow":
                    clock[0] += 16
                yield b"x" * (8 if case == "chunks" else 4)

        def close(self):
            closed.append(True)

    def respond(request):
        requests.append(request)
        assert request.extensions["timeout"]["read"] <= 10
        if case == "loop":
            clock[0] += 1
            return httpx.Response(302, headers={"Location": "/loop"}, stream=Stream())
        if case == "headers":
            clock[0] += 31
        headers = {"Content-Length": "21"} if case == "length" else {}
        if case == "encoding":
            headers["Content-Encoding"] = "gzip"
        return httpx.Response(
            500 if case == "status" else 200, headers=headers, stream=Stream()
        )

    transport(respond)
    monkeypatch.setattr(FeedParserRepository, "MAX_BYTES", 20, raising=False)
    message = {
        "length": "byte limit",
        "chunks": "byte limit",
        "slow": "time limit",
        "headers": "time limit",
        "loop": "too many redirects",
        "status": "Unable to fetch",
        "broken": "Unable to fetch",
        "encoding": "content encoding",
    }[case]
    with pytest.raises(FeedError, match=message):
        pdcst.subscribe(podcast.feed.url, "{title}", None, podcasts_repo=podcasts)
    assert before == {p: p.read_bytes() for p in tmp_path.rglob("*.json")}
    assert closed and len(requests) <= 6
    if case in {"length", "headers", "status", "encoding", "loop"}:
        assert not consumed
    if case == "slow":
        assert len(consumed) == 2


@pytest.mark.parametrize("location", [None, "/alternate/feed.xml"])
def test_relative_enclosure_and_http_metadata(transport, location):
    def respond(request):
        if request.url.path == "/feed":
            return httpx.Response(302, headers={"Location": "/base/feed.xml"})
        return httpx.Response(
            200,
            stream=httpx.ByteStream(BODY),
            headers={
                "Last-Modified": "Sun, 03 Jul 2022 12:00:00 GMT",
                **({"Content-Location": location} if location else {}),
            },
        )

    transport(respond)
    podcast, episodes = FeedParserRepository().get(
        "https://example.test/feed", None, "{title}"
    )
    assert episodes[0].audio_url == (
        "https://example.test/alternate/audio.mp3"
        if location
        else "https://example.test/base/audio.mp3"
    )
    assert podcast.feed.updated.day == 3
    assert podcast.feed.url == "https://example.test/feed"


PRIVATE_URL = (
    "https://USER_SECRET:PASS_SECRET@example.test/feed"
    "?credential=QUERY_SECRET#FRAGMENT_SECRET"
)


@pytest.mark.parametrize("entrypoint", ["fetch", "get", "subscribe"])
@pytest.mark.parametrize(
    "case,category",
    [
        ("status", "HTTP 403"),
        ("timeout", "timeout"),
        ("connection", "Unable to fetch"),
        ("invalid", "Unable to fetch"),
        ("value", "Unable to fetch"),
    ],
)
def test_feed_transport_errors_do_not_retain_private_causes(
    tmp_path, monkeypatch, transport, caplog, entrypoint, case, category
):
    monkeypatch.setattr(pdcst.settings, "root", tmp_path)

    class UnreadBody(httpx.SyncByteStream):
        def __iter__(self):
            pytest.fail("must not read failed response body")

    def respond(request):
        if case == "status":
            return httpx.Response(403, stream=UnreadBody())
        error_type = {
            "timeout": httpx.ReadTimeout,
            "connection": httpx.ConnectError,
            "invalid": httpx.InvalidURL,
            "value": ValueError,
        }[case]
        raise error_type(f"Private failure: {PRIVATE_URL}")

    transport(respond)
    with pytest.raises(FeedError) as caught:
        if entrypoint == "fetch":
            FeedParserRepository.fetch_document(PRIVATE_URL)
        elif entrypoint == "get":
            FeedParserRepository().get(PRIVATE_URL, None, "{title}")
        else:
            pdcst.subscribe(PRIVATE_URL, "{title}", None)
    error = caught.value
    rendered = str(error) + "".join(traceback.format_exception(error)) + caplog.text
    for marker in ("USER_SECRET", "PASS_SECRET", "QUERY_SECRET", "FRAGMENT_SECRET"):
        assert marker not in rendered
    assert category in str(error)
    assert error.__cause__ is None
    assert error.__context__ is None
    assert len(str(error)) < 150
    assert not list(tmp_path.rglob("*.json"))


@pytest.mark.parametrize(
    "tail",
    [
        "<item><guid>two</guid><title>Truncated",  # an earlier item is complete
        "</channel></rss><trailing",  # complete root, malformed trailing XML
        "</channel></rssx>",  # mismatched end tag
        # A recoverable entity error first must not hide a truncated tail.
        "<item><guid>two</guid><title>News &amp Views",
        "<item><guid>two</guid><title>A&nbsp;B</title></item></channel></rss><x",
        # ... nor a truncated item or mismatched tag before a complete root.
        "<item><guid>two</guid><title>A & B</channel></rss>",
        "<item><guid>two</guid><title>A&nbsp;B</titlex></item></channel></rss>",
        "<item><guid>two</guid><title>A & B</title><x:y/></item></channel></rss>",
    ],
)
def test_fatal_feed_xml_preserves_previous_generation(
    tmp_path, monkeypatch, transport, tail
):
    monkeypatch.setattr(pdcst.settings, "root", tmp_path)
    body = [BODY]
    transport(lambda request: httpx.Response(200, stream=httpx.ByteStream(body[0])))
    url = "https://example.test/feed"
    podcast = pdcst.subscribe(url, "{title}.mp3", None)
    repo = EpisodeRepository(tmp_path / podcast.directory)
    episode = repo.list()[0]
    episode.audio_file = "saved.mp3"
    repo.add(episode)
    audio = repo.base_dir / episode.audio_file
    audio.write_bytes(b"previous synthetic audio")
    before = {p: p.read_bytes() for p in tmp_path.rglob("*.json")}
    body[0] = (
        "<rss><channel><title>Changed</title>" + item(enclosures=ENCLOSURE) + tail
    ).encode()
    # Pin the parser oracle: this is a fatal structural error, yet a usable
    # complete first entry survives feedparser's lenient fallback.
    from xml.sax import SAXParseException

    document = feedparser.parse(body[0])
    assert document.bozo
    assert isinstance(document.bozo_exception, SAXParseException)
    assert document.entries[0].id == "one"
    with pytest.raises(FeedError, match="malformed or incomplete XML"):
        pdcst.subscribe(url, "{title}.mp3", None)
    assert before == {p: p.read_bytes() for p in tmp_path.rglob("*.json")}
    assert audio.read_bytes() == b"previous synthetic audio"
    assert not list(tmp_path.rglob(".pdcst-refresh-*"))


@pytest.mark.parametrize(
    "warning",
    ["content-type", "encoding", "ampersand", "entity", "header-charset", "sjis"],
)
def test_complete_feed_with_tolerated_parser_warning_is_accepted(
    tmp_path, monkeypatch, transport, warning
):
    monkeypatch.setattr(pdcst.settings, "root", tmp_path)
    if warning == "content-type":
        body = BODY
        headers = {"content-type": "text/plain"}
        expected = feedparser.NonXMLContentType
    elif warning == "encoding":
        body = BODY.replace(b"Synthetic", "Synthétic".encode())
        headers = {"content-type": "application/rss+xml; charset=us-ascii"}
        expected = feedparser.CharacterEncodingOverride
    else:
        # Recoverable well-formedness errors in a complete document remain
        # accepted: a bare ampersand or an HTML entity without a DOCTYPE.
        from xml.sax import SAXParseException

        headers = {}
        expected = SAXParseException
        if warning == "ampersand":
            body = BODY.replace(b"Synthetic", b"News & Views") + b"\n"
        elif warning == "entity":
            body = BODY.replace(b"Synthetic", b"News&nbsp;Views")
        elif warning == "header-charset":
            # No XML declaration; only the HTTP header names the charset.
            body = BODY.replace(b"Synthetic", "Café&nbsp;Views".encode("cp1252"))
            headers = {"content-type": "application/rss+xml; charset=windows-1252"}
        else:
            body = b'<?xml version="1.0" encoding="Shift_JIS"?>' + BODY.replace(
                b"Synthetic", "日本 & Views".encode("shift_jis")
            )
    document = feedparser.parse(body, response_headers=headers)
    assert document.bozo
    assert isinstance(document.bozo_exception, expected)
    transport(
        lambda request: httpx.Response(
            200, headers=headers, stream=httpx.ByteStream(body)
        )
    )
    podcast = pdcst.subscribe("https://example.test/feed", "{title}", None)
    assert podcast.episodes_count == 1
    assert EpisodeRepository(tmp_path / podcast.directory).list()[0].guid == (
        "https://example.test/one"
    )


@pytest.mark.parametrize("digits", ["9" * 5000, "0" * 5000 + "8388609"])
def test_overlong_feed_length_refuses_before_body_and_preserves_stores(
    tmp_path, monkeypatch, transport, podcast, episode, digits
):
    monkeypatch.setattr(pdcst.settings, "root", tmp_path)
    shows = PodcastRepository(tmp_path)
    shows.add(podcast)
    EpisodeRepository(tmp_path / podcast.directory).add(episode)
    before = {p: p.read_bytes() for p in tmp_path.rglob("*.json")}

    class UnreadBody(httpx.SyncByteStream):
        def __iter__(self):
            pytest.fail("over-limit length must be refused before reading")

    transport(
        lambda _: httpx.Response(
            200, headers={"Content-Length": digits}, stream=UnreadBody()
        )
    )
    with pytest.raises(FeedError, match="Feed exceeds byte limit") as caught:
        pdcst.subscribe(podcast.feed.url, "{title}", None, podcasts_repo=shows)
    assert caught.value.__cause__ is caught.value.__context__ is None
    assert len(str(caught.value)) < 100
    assert before == {p: p.read_bytes() for p in tmp_path.rglob("*.json")}


def test_zero_padded_feed_length_is_accepted(transport):
    transport(
        lambda _: httpx.Response(
            200,
            headers={"Content-Length": "0" * 5000 + str(len(BODY))},
            stream=httpx.ByteStream(BODY),
        )
    )
    assert FeedParserRepository.fetch_document("https://example.test/feed").entries
