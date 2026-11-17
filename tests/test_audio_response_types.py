"""Final response types are admitted before audio staging or publication."""

import importlib

import httpx
import pytest
from typer.testing import CliRunner

import pdcst
from pdcst.models import AUDIO_MIME_EXTENSIONS


@pytest.fixture
def response_type(monkeypatch):
    client = httpx.Client

    class Body(httpx.SyncByteStream):
        consumed = False
        closed = False

        def __iter__(self):
            self.consumed = True
            yield b"synthetic audio"

        def close(self):
            self.closed = True

    def install(content_type, *, redirect=False):
        body = Body()
        headers = {} if content_type is None else {"Content-Type": content_type}

        def respond(request):
            if redirect and request.url.path != "/final":
                # Admission uses the final type, not the redirect's declaration.
                return httpx.Response(
                    302, headers={"Location": "/final", "Content-Type": "audio/mpeg"}
                )
            return httpx.Response(200, headers=headers, stream=body)

        monkeypatch.setattr(
            httpx,
            "Client",
            lambda **kw: client(transport=httpx.MockTransport(respond), **kw),
        )
        return body

    return install


@pytest.mark.parametrize(
    "content_type",
    [
        "text/html",
        "TEXT/HTML; charset=UTF-8",
        "application/xhtml+xml",
        "application/json",
        "application/problem+json",
        "application/xml",
        "text/xml",
        "application/rss+xml",
        "text/plain",
        "image/png",
        "audio/x-unknown",
        "audio/mpeg, text/html",
        "; charset=utf-8",
    ],
)
def test_document_and_unsupported_types_are_refused_before_staging(
    tmp_path, response_type, content_type
):
    module = importlib.import_module("pdcst.download")
    body = response_type(content_type)
    with pytest.raises(module.DownloadError, match="Unsupported media content type"):
        module.download_with_progress(
            "https://example.test/start", tmp_path / "new" / "audio.mp3"
        )
    assert body.closed and not body.consumed
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize(
    "content_type",
    [
        *AUDIO_MIME_EXTENSIONS,
        "application/ogg",
        "application/mp4",
        "application/octet-stream",
        "binary/octet-stream",
        None,
        "",
        "  ",
        " Audio/MPEG ; charset=binary",
    ],
)
def test_supported_audio_containers_and_compatibility_types(
    tmp_path, response_type, content_type
):
    module = importlib.import_module("pdcst.download")
    body = response_type(content_type)
    target = tmp_path / "audio.mp3"
    module.download_with_progress("https://example.test/start", target)
    assert body.closed and body.consumed
    assert target.read_bytes() == b"synthetic audio"


@pytest.mark.parametrize("entrypoint", ["cli", "repository"])
@pytest.mark.parametrize(
    "content_type", ["text/html", "application/json", "application/xml"]
)
def test_final_error_document_preserves_existing_audio_and_metadata(
    publication_store, monkeypatch, response_type, entrypoint, content_type
):
    module = importlib.import_module("pdcst.download")
    home, episodes = publication_store
    monkeypatch.setattr(pdcst.settings, "root", home)
    episode = next(iter(episodes.all.values()))
    before = episodes.path.read_bytes()
    target = episodes.base_dir / "saved.mp3"
    body = response_type(content_type + "; private=SYNTHETIC_SECRET", redirect=True)

    if entrypoint == "cli":
        result = CliRunner().invoke(pdcst.cli, ["download", "python", "refactoring"])
        assert result.exit_code != 0
        assert "Unsupported media content type" in result.output
        assert "SYNTHETIC_SECRET" not in result.output
    else:
        with pytest.raises(module.DownloadError) as caught:
            episodes.download_episode(
                episode, target.name, module.download_with_progress
            )
        assert str(caught.value) == "Unsupported media content type"
    assert body.closed and not body.consumed
    assert target.read_bytes() == b"previous audio"
    assert episodes.path.read_bytes() == before
    assert not list(episodes.base_dir.glob(".pdcst-download-*"))
    assert not list(episodes.base_dir.glob("*.tmp"))

    response_type("application/octet-stream", redirect=True)
    episodes.download_episode(episode, target.name, module.download_with_progress)
    assert target.read_bytes() == b"synthetic audio"
    assert episodes.all[episode.guid].audio_file == target.name
