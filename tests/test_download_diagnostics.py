"""Failed synthetic signed-URL downloads expose only bounded diagnostics."""

import importlib
import traceback
from contextlib import contextmanager
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
        ("huge-declared", "Audio download byte limit exceeded"),
        ("chunked", "Audio download byte limit exceeded"),
        ("duration", "Audio download duration limit exceeded"),
        ("encoded", "Unsupported media content encoding"),
        ("short", "Incomplete media response"),
        ("partial", "Incomplete media response"),
        ("range-header", "Incomplete media response"),
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
    monkeypatch.setattr(pdcst, "find_podcast_by_identifier", lambda _, **kw: podcast)
    monkeypatch.setattr(pdcst, "find_episode_by_identifier", lambda *_, **kw: episode)

    class Stream(httpx.SyncByteStream):
        def __iter__(self):
            if case == "duration":
                clock[0] = 6
            yield b"part"
            if case == "chunked":
                yield b"too large"

    retry = [False]

    def respond(request):
        if retry[0]:
            return httpx.Response(200, stream=httpx.ByteStream(b"complete"))
        if case == "timeout":
            raise httpx.ReadTimeout(f"Private failure: {URL}", request=request)
        if case == "403":
            return httpx.Response(403)
        if case == "redirect":
            return httpx.Response(302, headers={"Location": URL})
        headers = {}
        if case == "declared":
            headers["Content-Length"] = "9"
        if case == "huge-declared":
            headers["Content-Length"] = "9" * 5000
        if case == "encoded":
            headers["Content-Encoding"] = "gzip"
        if case == "short":
            headers["Content-Length"] = "7"
        if case in {"partial", "range-header"}:
            headers.update({"Content-Length": "4", "Content-Range": "bytes 0-3/8"})
        return httpx.Response(
            206 if case == "partial" else 200, headers=headers, stream=Stream()
        )

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
    if case in {"partial", "range-header"}:
        retry[0] = True
        result = CliRunner().invoke(pdcst.cli, ["download", "show", "episode"])
        assert result.exit_code == 0, result.output
        assert target.read_bytes() == b"complete"


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


@pytest.mark.parametrize("case", ["interrupted", "replacement", "eof", "published"])
def test_response_close_failure_preserves_transfer_outcome(
    tmp_path, monkeypatch, caplog, case
):
    module = importlib.import_module("pdcst.download")
    target = tmp_path / "audio.mp3"
    target.write_bytes(b"old audio")
    close_failures = []

    def fail_close():
        close_failures.append(target.read_bytes())
        raise httpx.CloseError(f"Private close failure: {URL}")

    class Stream(httpx.SyncByteStream):
        def __iter__(self):
            yield b"complete"
            if case == "interrupted":
                raise httpx.ReadTimeout(f"Private read failure: {URL}")

        def close(self):
            if case != "published":
                fail_close()

    client = httpx.Client
    monkeypatch.setattr(
        httpx,
        "Client",
        lambda **kw: client(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(200, stream=Stream())
            ),
            **kw,
        ),
    )
    if case == "published":
        close = httpx.Response.close

        def close_after_publication(response):
            if response.is_closed:
                fail_close()
            close(response)

        monkeypatch.setattr(httpx.Response, "close", close_after_publication)

    if case == "replacement":
        replacement_file = module.replacement_file

        @contextmanager
        def fail_replacement(*args, **kwargs):
            with replacement_file(*args, **kwargs) as output:
                yield output
                raise OSError("synthetic replacement failure")

        monkeypatch.setattr(module, "replacement_file", fail_replacement)

    if case == "interrupted":
        with pytest.raises(
            module.DownloadError, match="Unable to fetch audio"
        ) as error:
            module.download_with_progress(URL, target)
        assert isinstance(error.value.__context__, httpx.ReadTimeout)
        assert "QUERY_SECRET" not in "".join(traceback.format_exception(error.value))
    elif case == "replacement":
        with pytest.raises(OSError, match="synthetic replacement failure"):
            module.download_with_progress(URL, target)
    else:
        assert module.download_with_progress(URL, target) == target

    assert close_failures == [b"complete" if case == "published" else b"old audio"]
    succeeded = case in {"eof", "published"}
    assert target.read_bytes() == (b"complete" if succeeded else b"old audio")
    assert list(tmp_path.iterdir()) == [target]
    assert ("Audio transferred; response close failed" in caplog.text) == succeeded
    assert "QUERY_SECRET" not in caplog.text
