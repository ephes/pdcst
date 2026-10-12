import copy
import os
import stat
import threading
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest

import pdcst
from pdcst.download import download_with_progress
from pdcst.models import Podcast
from pdcst.repository import PodcastRepository


@pytest.mark.parametrize(
    "title", ["../../victim", "/absolute", r"..\victim", "..", "☃"]
)
def test_feed_names_are_components(feed, title):
    podcast = Podcast(feed=feed, title=title, episodes_count=1)
    assert podcast.directory not in {"", ".", ".."}
    assert "/" not in podcast.directory and "\\" not in podcast.directory


@pytest.mark.parametrize(
    "directory", ["../escape", "/escape", r"C:\escape", r"..\escape"]
)
def test_directory_rejected(tmp_path, directory):
    with pytest.raises(ValueError):
        pdcst.contained_path(tmp_path, directory)


def test_symlink_rejected(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    (root / "link").symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(ValueError):
        pdcst.contained_path(root, "link", "audio.mp3")
    assert (
        pdcst.contained_path(root, "nested/show", "audio.mp3")
        == root / "nested/show/audio.mp3"
    )


@pytest.mark.parametrize("pattern", ["../{title}", "/absolute", r"x\{title}"])
def test_pattern_rejected(podcast, episode, pattern):
    podcast.file_name_pattern = pattern
    with pytest.raises(ValueError):
        podcast.get_audio_file_name(episode)


def test_episode_title_and_url(podcast, episode):
    episode.title = "../../victim"
    episode.audio_url = "https://example.test/audio.mp3?x=/escape"
    assert "/" not in podcast.get_audio_file_name(episode)
    assert podcast.get_audio_file_name(episode).endswith(".mp3")


class BrokenStream(httpx.SyncByteStream):
    def __iter__(self):
        yield b"partial"
        raise httpx.ReadError("disconnect")


@pytest.mark.parametrize(
    "case",
    ["404", "broken", "short", "loop", "missing", "invalid", "redirect", "success"],
)
def test_download_atomic(tmp_path, monkeypatch, case):
    target = tmp_path / "audio.mp3"
    target.write_bytes(b"old")
    requests = []

    def respond(request):
        requests.append(request)
        if case == "loop" or (case == "redirect" and request.url.path != "/final"):
            return httpx.Response(302, headers={"Location": "/final"})
        if case == "404":
            return httpx.Response(404, content=b"error")
        if case == "broken":
            return httpx.Response(200, stream=BrokenStream())
        headers = {}
        if case == "short":
            headers["Content-Length"] = "100"
        elif case == "invalid":
            headers["Content-Length"] = "oops"
        elif case == "success":
            headers["Content-Length"] = "3"
        return httpx.Response(200, headers=headers, stream=httpx.ByteStream(b"new"))

    client = httpx.Client
    monkeypatch.setattr(
        "httpx.Client",
        lambda **kw: client(transport=httpx.MockTransport(respond), **kw),
    )
    if case in {"404", "broken", "short", "loop"}:
        with pytest.raises((httpx.HTTPError, ValueError)):
            download_with_progress("https://example.test/audio", target)
        assert target.read_bytes() == b"old"
    else:
        assert download_with_progress("https://example.test/audio", target) == target
        assert target.read_bytes() == b"new"
    assert list(tmp_path.iterdir()) == [target]
    assert len(requests) <= 6


def test_download_failure_leaves_metadata(tmp_path, monkeypatch, podcast, episode):
    monkeypatch.setattr(pdcst.settings, "root", tmp_path)
    monkeypatch.setattr(pdcst, "find_podcast_by_identifier", lambda _: podcast)
    monkeypatch.setattr(pdcst, "find_episode_by_identifier", lambda *_: episode)

    def fail(*_):
        raise httpx.ReadError("disconnect")

    monkeypatch.setattr(pdcst, "download_with_progress", fail)
    with pytest.raises(httpx.ReadError):
        pdcst.download("python", "rest")
    assert episode.audio_file is None
    assert not list(tmp_path.iterdir())


def test_repository_cached_list(tmp_path, podcast):
    repo = PodcastRepository(tmp_path)
    assert repo.list() == []
    repo.add(podcast)
    assert repo.list() == [podcast]


@pytest.mark.parametrize("failure", ["serialize", "flush", "replace"])
def test_repository_failure_preserves_bytes(tmp_path, podcast, monkeypatch, failure):
    repo = PodcastRepository(tmp_path)
    repo.add(podcast)
    old = repo.path.read_bytes()

    def fail(*args, **kwargs):
        raise OSError("injected")

    if failure == "serialize":
        monkeypatch.setattr(repo.storage_model_list_type, "json", fail)
    else:
        monkeypatch.setattr(
            "pdcst.storage.os." + ("fsync" if failure == "flush" else "replace"),
            fail,
        )
    with pytest.raises(OSError, match="injected"):
        repo.add(podcast)
    assert repo.path.read_bytes() == old
    assert sorted(p.name for p in tmp_path.iterdir()) == [
        "podcasts.json",
        "podcasts.json.lock",
    ]


def test_concurrent_adds(tmp_path, podcast, monkeypatch):
    other = copy.deepcopy(podcast)
    other.feed.url += "?other"
    first_read = threading.Event()
    second_started = threading.Event()
    original = PodcastRepository._fetch_all

    def fetch(repo):
        result = original(repo)
        if threading.current_thread().name.endswith("_0"):
            first_read.set()
            assert second_started.wait(5)
        return result

    monkeypatch.setattr(PodcastRepository, "_fetch_all", fetch)

    def second():
        assert first_read.wait(5)
        second_started.set()
        PodcastRepository(tmp_path).add(other)

    with ThreadPoolExecutor(max_workers=2) as pool:
        a = pool.submit(PodcastRepository(tmp_path).add, podcast)
        b = pool.submit(second)
        a.result(timeout=10)
        b.result(timeout=10)
    assert len(PodcastRepository(tmp_path).list()) == 2


@pytest.mark.parametrize("stored", [None, "custom!", "show?!"])
def test_existing_subscription_directory(tmp_path, monkeypatch, podcast, stored):
    monkeypatch.setattr(pdcst.settings, "root", tmp_path)
    podcast.title = "Show?!"
    legacy = stored or "show?!"
    (tmp_path / legacy).mkdir()
    repo = PodcastRepository(tmp_path)
    podcast.directory = stored
    repo.add(podcast)
    assert repo.list()[0].directory == legacy
    fresh = Podcast(feed=podcast.feed, title=podcast.title, episodes_count=0)

    class Parser:
        def get(self, *_):
            return fresh, []

    result = pdcst.subscribe(podcast.feed.url, "{title}", None, Parser(), repo)
    assert result.directory == legacy
    assert (tmp_path / legacy / "episodes.json").exists()
    assert not (tmp_path / "show_").exists()


@pytest.mark.parametrize("escaping", [False, True])
def test_legacy_directory_lookup_contained(tmp_path, monkeypatch, podcast, escaping):
    root = tmp_path / "root"
    root.mkdir()
    monkeypatch.setattr(pdcst.settings, "root", root)
    podcast.title = "Show!"
    podcast.directory = Podcast.title_for_filename(podcast.title)
    if escaping:
        (root / "show!").symlink_to(tmp_path, target_is_directory=True)
    else:
        (root / "show!").mkdir()

    class Parser:
        def get(self, *_):
            return podcast, []

    result = pdcst.subscribe("url", "{title}", None, Parser(), PodcastRepository(root))
    assert result.directory == ("show_" if escaping else "show!")
    assert not (tmp_path / "episodes.json").exists()


@pytest.mark.parametrize("kind", ["media", "json"])
@pytest.mark.parametrize("existing", [False, True])
def test_atomic_file_permissions(tmp_path, monkeypatch, podcast, kind, existing):
    repo = PodcastRepository(tmp_path)
    target = repo.path if kind == "json" else tmp_path / "audio.mp3"
    if existing:
        if kind == "json":
            repo.add(podcast)
        else:
            target.write_bytes(b"old")
        target.chmod(0o664)
    client = httpx.Client
    monkeypatch.setattr(
        "httpx.Client",
        lambda **kw: client(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(200, stream=httpx.ByteStream(b"new"))
            ),
            **kw
        ),
    )
    previous = os.umask(0o027)
    try:
        if kind == "json":
            repo.add(podcast)
        else:
            download_with_progress("https://example.test/audio", target)
    finally:
        os.umask(previous)
    assert stat.S_IMODE(target.stat().st_mode) == (0o664 if existing else 0o640)
