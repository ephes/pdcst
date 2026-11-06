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
