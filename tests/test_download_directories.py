"""Directory replacement barriers use only synthetic audio and metadata."""

import os
from contextlib import contextmanager

import httpx
import pytest

from pdcst import repository
from pdcst.repository import DownloadConflict


@pytest.mark.parametrize("transport", ["callback", "http"])
@pytest.mark.parametrize("component", ["episode", "ancestor", "root"])
@pytest.mark.parametrize(
    "when", ["before", "claim", "admission", "transfer", "intent", "publish"]
)
def test_replaced_directory_never_redirects_download(
    publication_store, episode, monkeypatch, component, when, transport
):
    home, repo = publication_store
    monkeypatch.setattr(repository.settings, "root", home)
    # Add a distinct ancestor so every boundary can be replaced independently.
    nested = home / "shows"
    nested.mkdir()
    original_dir = repo.base_dir
    original_dir.rename(nested / original_dir.name)
    repo = repository.EpisodeRepository(nested / original_dir.name)
    before = repo.path.read_bytes()
    victim = {"episode": repo.base_dir, "ancestor": nested, "root": home}[component]
    outside = home.with_name(home.name + "-outside")
    outside.mkdir()
    # A populated outside tree would previously be read/overwritten on publication.
    relative = repo.base_dir.relative_to(victim)
    outside_episode = outside / relative
    outside_episode.mkdir(parents=True, exist_ok=True)
    (outside_episode / repo.name).write_bytes(before)
    (outside_episode / "saved.mp3").write_bytes(b"outside audio")
    snapshot = {
        p.relative_to(outside): p.read_bytes()
        for p in outside.rglob("*")
        if p.is_file()
    }
    moved = victim.with_name(victim.name + "-admitted")
    replaced = []
    calls = []

    def swap():
        if not replaced:
            victim.rename(moved)
            victim.symlink_to(outside, target_is_directory=True)
            replaced.append(True)

    lock = repository.advisory_lock

    @contextmanager
    def at_claim(path, **kwargs):
        if when == "claim" and path.name.endswith(".download.lock"):
            swap()
        with lock(path, **kwargs):
            yield

    record = repository.EpisodeRepository._download_record

    def at_admission(self, *args, **kwargs):
        result = record(self, *args, **kwargs)
        if when == "admission":
            swap()
        return result

    intent = repository.write_intent

    def at_intent(path, details, **kwargs):
        result = intent(path, details, **kwargs)
        if when == "intent":
            swap()
        return result

    replace = repository.os.replace

    def at_publish(source, target, **kwargs):
        if when == "publish" and str(target).endswith("saved.mp3"):
            swap()
        return replace(source, target, **kwargs)

    def transfer(url, stage, *, directory_fd=None):
        calls.append(True)
        if when == "transfer":
            swap()
        fd = os.open(
            stage if directory_fd is None else stage.name,
            os.O_WRONLY | os.O_TRUNC | os.O_NOFOLLOW,
            dir_fd=directory_fd,
        )
        with os.fdopen(fd, "wb") as output:
            output.write(b"completed audio")

    selected_transfer = transfer
    if transport == "http":
        from pdcst.download import download_with_progress

        client = httpx.Client

        def response(request):
            calls.append(True)
            if when == "transfer":
                swap()
            return httpx.Response(200, stream=httpx.ByteStream(b"completed audio"))

        monkeypatch.setattr(
            httpx,
            "Client",
            lambda **kwargs: client(transport=httpx.MockTransport(response), **kwargs),
        )
        selected_transfer = download_with_progress

    monkeypatch.setattr(repository, "advisory_lock", at_claim)
    monkeypatch.setattr(repository.EpisodeRepository, "_download_record", at_admission)
    monkeypatch.setattr(repository, "write_intent", at_intent)
    monkeypatch.setattr(repository.os, "replace", at_publish)
    if when == "before":
        swap()
    with pytest.raises(DownloadConflict, match="directory.*changed"):
        repo.download_episode(episode, "saved.mp3", selected_transfer)
    assert replaced
    assert len(calls) == (when in {"transfer", "intent", "publish"})
    assert {
        p.relative_to(outside): p.read_bytes()
        for p in outside.rglob("*")
        if p.is_file()
    } == snapshot
    admitted = moved / relative
    assert (admitted / repo.name).read_bytes() == before
    assert (admitted / "saved.mp3").read_bytes() == b"previous audio"
