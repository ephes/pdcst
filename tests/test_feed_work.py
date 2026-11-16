"""Refuse expensive XML before feedparser builds an expanded document."""

import httpx
import pytest

import pdcst
from pdcst.repository import (
    EpisodeRepository,
    FeedError,
    FeedParserRepository,
    PodcastRepository,
)
from tests.test_feed_transport import BODY


@pytest.mark.parametrize(
    "case",
    [
        "depth",
        "nodes",
        "entries",
        "links",
        "attributes",
        "namespaces",
        "doctype",
        "external-doctype",
        "repaired-depth",
    ],
)
def test_xml_work_refused_before_materialization(
    tmp_path, monkeypatch, podcast, episode, case
):
    monkeypatch.setattr(pdcst.settings, "root", tmp_path)
    shows = PodcastRepository(tmp_path)
    shows.add(podcast)
    episodes = EpisodeRepository(tmp_path / podcast.directory)
    episodes.add(episode)
    before = {p: p.read_bytes() for p in tmp_path.rglob("*.json")}
    monkeypatch.setattr(FeedParserRepository, "MAX_XML_DEPTH", 8, raising=False)
    monkeypatch.setattr(FeedParserRepository, "MAX_XML_NODES", 30, raising=False)
    monkeypatch.setattr(FeedParserRepository, "MAX_XML_ENTRIES", 2, raising=False)
    monkeypatch.setattr(FeedParserRepository, "MAX_XML_LINKS", 2, raising=False)
    monkeypatch.setattr(FeedParserRepository, "MAX_XML_ATTRIBUTES", 4, raising=False)
    payload = {
        "depth": "<x>" * 9 + "</x>" * 9,
        "repaired-depth": "A & B" + "<x>" * 9 + "</x>" * 9,
        "nodes": "<x/>" * 31,
        "entries": "<item/>" * 3,
        "links": "<link/>" * 3,
        "attributes": '<x a="1" b="2" c="3" d="4" e="5"/>',
        "namespaces": '<x xmlns:a="a" xmlns:b="b" xmlns:c="c" xmlns:d="d" xmlns:e="e"/>',
    }.get(case, "")
    body = BODY.replace(b"</channel>", payload.encode() + b"</channel>")
    if case == "doctype":
        body = b'<!DOCTYPE rss [<!ENTITY a "expanded">]>' + body
    if case == "external-doctype":
        body = b'<!DOCTYPE rss SYSTEM "https://example.test/private.dtd">' + body
    client = httpx.Client
    monkeypatch.setattr(
        httpx,
        "Client",
        lambda **kw: client(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(200, stream=httpx.ByteStream(body))
            ),
            **kw,
        ),
    )
    monkeypatch.setattr(
        "feedparser.parse",
        lambda *_a, **_kw: pytest.fail(
            "feedparser materialized an unadmitted document"
        ),
    )
    with pytest.raises(FeedError, match="XML.*(limit|DOCTYPE)"):
        pdcst.subscribe(podcast.feed.url, "{index}.mp3", None, podcasts_repo=shows)
    assert before == {p: p.read_bytes() for p in tmp_path.rglob("*.json")}
    assert not list(tmp_path.rglob(".pdcst-refresh-*"))


@pytest.mark.parametrize("encoding", ["utf-8", "utf-16", "shift_jis"])
def test_preflight_exact_limits_and_namespaced_atom(monkeypatch, encoding):
    monkeypatch.setattr(FeedParserRepository, "MAX_XML_DEPTH", 3)
    monkeypatch.setattr(FeedParserRepository, "MAX_XML_NODES", 4)
    monkeypatch.setattr(FeedParserRepository, "MAX_XML_ENTRIES", 1)
    monkeypatch.setattr(FeedParserRepository, "MAX_XML_LINKS", 1)
    monkeypatch.setattr(FeedParserRepository, "MAX_XML_ATTRIBUTES", 2)
    text = (
        '<?xml version="1.0" encoding="'
        + encoding
        + '"?><feed xmlns="http://www.w3.org/2005/Atom">'
        '<entry><link href="audio.mp3"/><title>News & Views</title></entry></feed>'
    )
    FeedParserRepository.preflight_xml(text.encode(encoding), {}, lambda: None)
    with pytest.raises(FeedError, match="XML work limit"):
        FeedParserRepository.preflight_xml(
            text.replace("</entry>", "<link/></entry>").encode(encoding),
            {},
            lambda: None,
        )


def test_preflight_repaired_bytes_and_deadline(monkeypatch):
    body = b"<rss>" + b"&" * 100 + b"</rss>"
    monkeypatch.setattr(FeedParserRepository, "MAX_XML_BYTES", 300)
    with pytest.raises(FeedError, match="XML repaired byte limit"):
        FeedParserRepository.preflight_xml(body, {}, lambda: None)
    calls = []

    def expired():
        calls.append(True)
        if len(calls) == 2:
            raise FeedError("Feed fetch exceeded time limit")

    with pytest.raises(FeedError, match="time limit"):
        FeedParserRepository.preflight_xml(BODY, {}, expired)
