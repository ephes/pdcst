"""Redirect bodies are discarded without escaping the audio transfer budget."""

import importlib
from types import SimpleNamespace

import httpx
import pytest
from typer.testing import CliRunner

import pdcst


class RedirectBody(httpx.SyncByteStream):
    def __init__(self, on_close=lambda: None):
        self.consumed = 0
        self.closed = False
        self.on_close = on_close

    def __iter__(self):
        self.consumed += 40
        yield b"x" * 40

    def close(self):
        self.closed = True
        self.on_close()


@pytest.fixture
def transport(monkeypatch):
    module = importlib.import_module("pdcst.download")
    monkeypatch.setattr(
        module, "settings", SimpleNamespace(audio_max_bytes=8, audio_max_seconds=5)
    )
    clock = [0]
    monkeypatch.setattr(module, "monotonic", lambda: clock[0])
    client = httpx.Client

    def install(respond):
        monkeypatch.setattr(
            httpx,
            "Client",
            lambda **kw: client(transport=httpx.MockTransport(respond), **kw),
        )

    return module, clock, install


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_five_redirects_close_unread_bodies_and_preserve_httpx_requests(
    tmp_path, transport, status
):
    module, clock, install = transport
    bodies = []
    requests = []

    def respond(request):
        assert all(body.closed for body in bodies)
        requests.append(request)
        clock[0] += 0.5
        if len(requests) == 6:
            return httpx.Response(200, stream=httpx.ByteStream(b"audio"))
        body = RedirectBody()
        bodies.append(body)
        return httpx.Response(
            status,
            headers={
                "Location": (
                    "https://cdn.example.test/folder/start"
                    if len(requests) == 1
                    else f"../folder/hop{len(requests)}?signed=synthetic"
                ),
                "Set-Cookie": "enclosure=synthetic; Path=/",
            },
            stream=body,
        )

    install(respond)
    target = tmp_path / "audio.mp3"
    module.download_with_progress("https://user:pass@example.test/start", target)
    assert target.read_bytes() == b"audio"
    assert all(body.closed and body.consumed == 0 for body in bodies)
    assert len(requests) == 6
    assert str(requests[-1].url) == (
        "https://cdn.example.test/folder/hop5?signed=synthetic"
    )
    assert "authorization" in requests[0].headers
    assert "authorization" not in requests[1].headers
    assert "enclosure=synthetic" in requests[-1].headers["cookie"]
    for index, request in enumerate(requests):
        assert request.headers["Accept-Encoding"] == "identity"
        assert all(
            0 < timeout <= 5 - index * 0.5
            for timeout in request.extensions["timeout"].values()
        )


@pytest.mark.parametrize("case", ["accepted", "missing", "limit"])
def test_redirect_close_failure_preserves_redirect_outcome(
    tmp_path, monkeypatch, caplog, transport, case
):
    module, _, install = transport
    target = tmp_path / "audio.mp3"
    target.write_bytes(b"previous audio")
    requests = []
    bodies = []

    def fail_close():
        raise httpx.CloseError("private redirect close failure")

    def respond(request):
        requests.append(request.url.path)
        if case == "accepted" and request.url.path == "/next":
            return httpx.Response(200, stream=httpx.ByteStream(b"audio"))
        body = RedirectBody(fail_close)
        bodies.append(body)
        return httpx.Response(302, headers={"Location": "/next"}, stream=body)

    if case == "missing":
        send = httpx.Client.send

        def send_without_next_request(client, *args, **kwargs):
            response = send(client, *args, **kwargs)
            response.next_request = None
            return response

        monkeypatch.setattr(httpx.Client, "send", send_without_next_request)

    install(respond)
    url = "https://example.test/start"
    if case == "accepted":
        assert module.download_with_progress(url, target) == target
        assert requests == ["/start", "/next"]
        assert target.read_bytes() == b"audio"
    else:
        message = (
            "Audio redirect did not prepare a request"
            if case == "missing"
            else "Unable to fetch audio"
        )
        with pytest.raises(module.DownloadError, match=message) as error:
            module.download_with_progress(url, target)
        if case == "limit":
            assert isinstance(error.value.__context__, httpx.TooManyRedirects)
        assert requests == (
            ["/start"] if case == "missing" else ["/start"] + ["/next"] * 5
        )
        assert target.read_bytes() == b"previous audio"

    assert all(body.closed and body.consumed == 0 for body in bodies)
    expected_warnings = {"accepted": 1, "missing": 0, "limit": 5}[case]
    warning = "Audio redirect accepted; response close failed"
    assert caplog.text.count(warning) == expected_warnings
    assert "private redirect close failure" not in caplog.text


@pytest.mark.parametrize("entrypoint", ["cli", "repository"])
@pytest.mark.parametrize(
    "case,expected_requests,message",
    [
        ("headers", 1, "duration limit"),
        ("chain", 2, "duration limit"),
        ("close", 1, "duration limit"),
        ("hops", 6, "Unable to fetch audio"),
    ],
)
def test_redirect_refusal_preserves_publication_and_allows_retry(
    publication_store,
    monkeypatch,
    transport,
    entrypoint,
    case,
    expected_requests,
    message,
):
    module, clock, install = transport
    home, episodes = publication_store
    monkeypatch.setattr(pdcst.settings, "root", home)
    episode = next(iter(episodes.all.values()))
    before = episodes.path.read_bytes()
    target = episodes.base_dir / "saved.mp3"
    bodies = []
    requests = []
    retry = [False]

    def respond(request):
        requests.append(request)
        if retry[0]:
            return httpx.Response(200, stream=httpx.ByteStream(b"audio"))
        if case == "headers":
            clock[0] = 6
        if case == "chain":
            clock[0] += 3

        def close():
            if case == "close":
                clock[0] = 6

        body = RedirectBody(close)
        bodies.append(body)
        return httpx.Response(302, headers={"Location": "/next"}, stream=body)

    install(respond)

    def download():
        if entrypoint == "repository":
            return episodes.download_episode(
                episode, target.name, module.download_with_progress
            )
        result = CliRunner().invoke(pdcst.cli, ["download", "python", "refactoring"])
        if result.exit_code:
            assert message in result.output
            raise module.DownloadError(message)

    with pytest.raises(module.DownloadError, match=message):
        download()
    assert len(requests) == expected_requests
    assert all(body.closed and body.consumed == 0 for body in bodies)
    assert target.read_bytes() == b"previous audio"
    assert episodes.path.read_bytes() == before
    assert not list(episodes.base_dir.glob(".pdcst-download-*"))
    assert not list(episodes.base_dir.glob("*.tmp"))
    retry[0] = True
    clock[0] = 0
    download()
    assert target.read_bytes() == b"audio"
    assert episodes.all[episode.guid].audio_file == target.name
