import importlib
from types import SimpleNamespace

import httpx
import pytest
import typer
from pydantic import ValidationError

import pdcst
from pdcst.config import Settings
from pdcst.repository import EpisodeRepository


@pytest.mark.parametrize("case", ["declared", "chunked", "trickle", "headers", "eof"])
def test_audio_budget_preserves_audio_and_metadata(
    tmp_path, monkeypatch, podcast, episode, case
):
    module = importlib.import_module("pdcst.download")
    monkeypatch.setattr(pdcst.settings, "root", tmp_path)
    monkeypatch.setattr(
        module,
        "settings",
        SimpleNamespace(audio_max_bytes=8, audio_max_seconds=5),
        raising=False,
    )
    clock = [0]
    monkeypatch.setattr(module, "monotonic", lambda: clock[0], raising=False)
    monkeypatch.setattr(pdcst, "find_podcast_by_identifier", lambda _, **kw: podcast)
    monkeypatch.setattr(pdcst, "find_episode_by_identifier", lambda *_, **kw: episode)
    repo = EpisodeRepository(tmp_path / podcast.directory)
    episode.audio_file = podcast.get_audio_file_name(episode)
    repo.add(episode)
    before = repo.path.read_bytes()
    target = repo.base_dir / podcast.get_audio_file_name(episode)
    target.write_bytes(b"old")

    class Stream(httpx.SyncByteStream):
        consumed = 0
        closed = False

        def __iter__(self):
            for _ in range(3 if case == "chunked" else 1):
                self.consumed += 1
                if case == "trickle":
                    clock[0] = 6
                yield b"abcd"
            if case == "eof":
                clock[0] = 6

        def close(self):
            self.closed = True

    stream = Stream()

    def respond(request):
        if case == "headers":
            clock[0] = 6
        return httpx.Response(
            200,
            headers={"Content-Length": "9"} if case == "declared" else {},
            stream=stream,
        )

    client = httpx.Client
    monkeypatch.setattr(
        httpx,
        "Client",
        lambda **kw: client(transport=httpx.MockTransport(respond), **kw),
    )
    with pytest.raises(typer.BadParameter, match="Audio download.*limit"):
        pdcst.download("synthetic", "synthetic")
    assert target.read_bytes() == b"old"
    assert repo.path.read_bytes() == before
    assert episode.audio_file == target.name
    assert not list(tmp_path.rglob("*.tmp"))
    assert stream.closed
    if case in {"declared", "headers"}:
        assert stream.consumed == 0


@pytest.mark.parametrize(
    "field,value",
    [
        ("audio_max_bytes", 0),
        ("audio_max_bytes", -1),
        ("audio_max_seconds", 0),
        ("audio_max_seconds", -1),
        ("audio_max_seconds", float("nan")),
        ("audio_max_seconds", float("inf")),
    ],
)
def test_audio_budget_configuration(tmp_path, field, value):
    with pytest.raises(ValidationError):
        Settings(root=tmp_path, **{field: value})


def test_audio_budget_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("PDCST_AUDIO_MAX_BYTES", "123")
    monkeypatch.setenv("PDCST_AUDIO_MAX_SECONDS", "456")
    configured = Settings(root=tmp_path)
    assert configured.audio_max_bytes == 123
    assert configured.audio_max_seconds == 456


@pytest.mark.parametrize("length", ["9" * 5000, "0" * 5000 + "9"])
def test_huge_declared_length_is_controlled_and_preserves_audio(
    tmp_path, monkeypatch, length
):
    module = importlib.import_module("pdcst.download")
    monkeypatch.setattr(
        module, "settings", SimpleNamespace(audio_max_bytes=8, audio_max_seconds=5)
    )
    client = httpx.Client
    consumed = []

    class Stream(httpx.SyncByteStream):
        def __iter__(self):
            consumed.append(True)
            yield b"data"

    monkeypatch.setattr(
        httpx,
        "Client",
        lambda **kw: client(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200, headers={"Content-Length": length}, stream=Stream()
                )
            ),
            **kw,
        ),
    )
    target = tmp_path / "audio.mp3"
    target.write_bytes(b"old")
    with pytest.raises(module.DownloadError, match="byte limit"):
        module.download_with_progress(
            "https://example.test/audio?secret=synthetic", target
        )
    assert target.read_bytes() == b"old"
    assert not consumed
    assert list(tmp_path.iterdir()) == [target]


@pytest.mark.parametrize(
    "length,expected",
    [
        ("0" * 5000 + "4", 4),
        ("4", 4),
        ("8", 8),
        ("0", 0),
        ("-1", None),
        ("4.0", None),
        ("٤", None),
        ("²", None),
        ("4x", None),
    ],
)
def test_declared_length_numeric_domain(tmp_path, monkeypatch, length, expected):
    module = importlib.import_module("pdcst.download")
    monkeypatch.setattr(
        module, "settings", SimpleNamespace(audio_max_bytes=8, audio_max_seconds=5)
    )
    client = httpx.Client
    monkeypatch.setattr(
        httpx,
        "Client",
        lambda **kw: client(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200,
                    headers=httpx.Headers({"Content-Length": length}, encoding="utf-8"),
                    stream=httpx.ByteStream(b"data"),
                )
            ),
            **kw,
        ),
    )
    totals = []
    original_add_task = module.rich.progress.Progress.add_task

    def add_task(self, description, **kw):
        totals.append(kw["total"])
        return original_add_task(self, description, **kw)

    monkeypatch.setattr(module.rich.progress.Progress, "add_task", add_task)
    target = tmp_path / "audio.mp3"
    target.write_bytes(b"old")
    if expected in {0, 8}:
        with pytest.raises(module.DownloadError, match="Incomplete"):
            module.download_with_progress("https://example.test/audio", target)
        assert target.read_bytes() == b"old"
    else:
        module.download_with_progress("https://example.test/audio", target)
        assert target.read_bytes() == b"data"
    assert totals == [expected]
    assert list(tmp_path.iterdir()) == [target]
