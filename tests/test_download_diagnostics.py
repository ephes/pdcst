"""Failed synthetic signed-URL downloads expose only bounded diagnostics."""

import importlib
import traceback
from types import SimpleNamespace

import httpx
import pytest
from typer.testing import CliRunner

import pdcst
from pdcst.repository import EpisodeRepository

URL = (
    "https://USER_SECRET:PASS_SECRET@example.test/audio.mp3"
    "?credential=QUERY_SECRET#FRAGMENT_SECRET"
)


@pytest.mark.parametrize(
    "case,message",
    [
        ("403", "Audio download failed (HTTP 403)"),
        ("timeout", "Unable to fetch audio"),
        ("redirect", "Unable to fetch audio"),
        ("declared", "Audio download byte limit exceeded"),
        ("chunked", "Audio download byte limit exceeded"),
        ("duration", "Audio download duration limit exceeded"),
        ("encoded", "Unsupported media content encoding"),
        ("short", "Incomplete media response"),
    ],
)
def test_cli_download_errors_are_private_and_preserve_state(
    tmp_path, monkeypatch, podcast, episode, case, message
):
    module = importlib.import_module("pdcst.download")
    monkeypatch.setattr(pdcst.settings, "root", tmp_path)
    monkeypatch.setattr(
        module, "settings", SimpleNamespace(audio_max_bytes=8, audio_max_seconds=5)
    )
    clock = [0]
    monkeypatch.setattr(module, "monotonic", lambda: clock[0])
    episode.audio_url = URL
    episode.audio_file = "saved.mp3"
    podcast.file_name_pattern = "saved.mp3"
    repo = EpisodeRepository(tmp_path / podcast.directory)
    repo.add(episode)
    target = repo.base_dir / "saved.mp3"
    target.write_bytes(b"old audio")
    before = repo.path.read_bytes()
    monkeypatch.setattr(pdcst, "find_podcast_by_identifier", lambda _: podcast)
    monkeypatch.setattr(pdcst, "find_episode_by_identifier", lambda *_: episode)

    class Stream(httpx.SyncByteStream):
        def __iter__(self):
            if case == "duration":
                clock[0] = 6
            yield b"part"
            if case == "chunked":
                yield b"too large"

    def respond(request):
        if case == "timeout":
            raise httpx.ReadTimeout(f"Private failure: {URL}", request=request)
        if case == "403":
            return httpx.Response(403)
        if case == "redirect":
            return httpx.Response(302, headers={"Location": URL})
        headers = {}
        if case == "declared":
            headers["Content-Length"] = "9"
        if case == "encoded":
            headers["Content-Encoding"] = "gzip"
        if case == "short":
            headers["Content-Length"] = "7"
        return httpx.Response(200, headers=headers, stream=Stream())

    client = httpx.Client
    monkeypatch.setattr(
        httpx,
        "Client",
        lambda **kw: client(transport=httpx.MockTransport(respond), **kw),
    )
    result = CliRunner().invoke(pdcst.cli, ["download", "show", "episode"], color=False)
    assert result.exit_code != 0
    assert message in result.output
    rendered = result.output + "".join(traceback.format_exception(result.exception))
    assert "Traceback" not in result.output
    assert len(result.output) < 1500
    for marker in ("USER_SECRET", "PASS_SECRET", "QUERY_SECRET", "FRAGMENT_SECRET"):
        assert marker not in rendered
    assert target.read_bytes() == b"old audio"
    assert repo.path.read_bytes() == before
    assert not list(repo.base_dir.glob(".pdcst-download-*"))
    assert not list(repo.base_dir.glob("*.tmp"))


def test_transport_api_exception_chain_omits_private_values(tmp_path, monkeypatch):
    module = importlib.import_module("pdcst.download")
    client = httpx.Client

    def respond(request):
        raise httpx.ReadTimeout(f"Private failure: {URL}", request=request)

    monkeypatch.setattr(
        httpx,
        "Client",
        lambda **kw: client(transport=httpx.MockTransport(respond), **kw),
    )
    with pytest.raises(ValueError, match="Unable to fetch audio") as caught:
        module.download_with_progress(URL, tmp_path / "audio.mp3")
    rendered = "".join(traceback.format_exception(caught.value))
    assert "QUERY_SECRET" not in rendered
    assert "Private failure" not in rendered
    assert not list(tmp_path.iterdir())
