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


def _transfer(url, stage, *, directory_fd):
    from pdcst.storage import replacement_file

    with replacement_file(stage, binary=True, directory_fd=directory_fd) as output:
        output.write(b"completed audio")


@pytest.mark.skipif(
    not (getattr(os, "O_PATH", 0) or getattr(os, "O_SEARCH", 0)),
    reason="platform has no search-only directory descriptors",
)
@pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses directory permissions")
def test_search_only_ancestor_is_admitted(publication_store, episode):
    home, repo = publication_store
    # Pathname traversal needs only search permission on ancestors.
    home.chmod(0o311)
    try:
        repo.download_episode(episode, "saved.mp3", _transfer)
    finally:
        home.chmod(0o755)
    assert (repo.base_dir / "saved.mp3").read_bytes() == b"completed audio"


@pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses directory permissions")
def test_unreadable_episode_directory_reports_access_failure(
    publication_store, episode
):
    _, repo = publication_store
    before = repo.path.read_bytes()
    repo.base_dir.chmod(0o311)
    try:
        with pytest.raises(DownloadConflict) as raised:
            repo.download_episode(episode, "saved.mp3", _transfer)
    finally:
        repo.base_dir.chmod(0o755)
    message = str(raised.value)
    assert "cannot be opened or created" in message
    assert os.strerror(13) in message
    assert "identity changed" not in message
    assert repo.path.read_bytes() == before
    assert (repo.base_dir / "saved.mp3").read_bytes() == b"previous audio"


def test_symlinked_library_path_is_refused_without_writes(publication_store, episode):
    home, repo = publication_store
    before = repo.path.read_bytes()
    alias = home.with_name(home.name + "-alias")
    alias.symlink_to(home, target_is_directory=True)
    aliased = repository.EpisodeRepository(alias / repo.base_dir.name)
    with pytest.raises(DownloadConflict, match="symlinked.*symlink-free path"):
        aliased.download_episode(episode, "saved.mp3", _transfer)
    assert repo.path.read_bytes() == before
    assert (repo.base_dir / "saved.mp3").read_bytes() == b"previous audio"
    assert not list(repo.base_dir.glob(".pdcst-download-*"))


def test_concurrently_created_component_is_reopened(tmp_path, monkeypatch):
    from pdcst import storage

    mkdir = os.mkdir

    def racing_mkdir(name, *args, **kwargs):
        # Another process creates the same missing component first.
        mkdir(name, *args, **kwargs)
        raise FileExistsError(17, os.strerror(17))

    monkeypatch.setattr(storage.os, "mkdir", racing_mkdir)
    target = tmp_path / "new" / "episodes"
    with storage.admitted_directory(target) as (descriptor, check):
        check()
        assert os.fstat(descriptor).st_ino == os.stat(target).st_ino


def test_unusable_parent_reports_bounded_store_error(tmp_path):
    from pdcst.repository import StoreReadError

    plain = tmp_path / "plain"
    plain.write_bytes(b"not a directory")
    with pytest.raises(StoreReadError):
        repository.EpisodeRepository(plain / "episodes").all


def test_download_retries_unsynced_episode_directory_link(
    tmp_path, episode, monkeypatch
):
    from pdcst import storage

    root = tmp_path.resolve()
    source = repository.EpisodeRepository(root)
    episode.audio_url = "https://example.com/audio.mp3"
    source.add(episode)
    metadata = source.path.read_bytes()
    parent = root / "shows"
    destination = parent / "episodes"
    target = destination / "saved.mp3"
    events = []
    fail_sync = True
    mkdir = os.mkdir
    sync = storage.sync_directory
    root_info = root.stat()

    def create(name, *args, **kwargs):
        mkdir(name, *args, **kwargs)
        events.append(("mkdir", name))
        if name == destination.name:
            # Simulate an episode catalog supplied by another writer.
            (destination / source.name).write_bytes(metadata)

    def sync_link(path, *, directory_fd=None):
        info = os.fstat(directory_fd)
        if (info.st_dev, info.st_ino) == (root_info.st_dev, root_info.st_ino):
            events.append(("sync", root.name))
            if fail_sync:
                raise OSError("synthetic parent sync failure")
        elif parent.exists() and os.path.samestat(info, parent.stat()):
            events.append(("sync", parent.name))
        sync(path, directory_fd=directory_fd)

    def transfer(url, stage, *, directory_fd):
        events.append(("transfer", stage.parent.name))
        _transfer(url, stage, directory_fd=directory_fd)

    monkeypatch.setattr(os, "mkdir", create)
    monkeypatch.setattr(storage, "sync_directory", sync_link)

    repo = repository.EpisodeRepository(destination)
    with pytest.raises(DownloadConflict, match="directory sync failed"):
        repo.download_episode(episode, target.name, transfer)
    assert events == [("mkdir", parent.name), ("sync", root.name)]
    assert parent.is_dir()
    assert not destination.exists()

    events.clear()
    fail_sync = False
    retry = repository.EpisodeRepository(destination)
    assert retry.download_episode(episode, target.name, transfer) == target
    assert events == [
        ("sync", root.name),
        ("mkdir", destination.name),
        ("sync", parent.name),
        ("transfer", destination.name),
    ]
    assert target.read_bytes() == b"completed audio"
    assert retry.list()[0].audio_file == target.name


@pytest.mark.parametrize("new_link", [False, True])
def test_unsyncable_ancestor_fails_only_for_new_links(
    tmp_path, episode, monkeypatch, new_link
):
    import errno

    from pdcst import storage

    root = tmp_path.resolve()
    source = repository.EpisodeRepository(root)
    episode.audio_url = "https://example.com/audio.mp3"
    source.add(episode)
    destination = root / "episodes"
    if not new_link:
        destination.mkdir()
    sync = storage.sync_directory
    root_info = root.stat()

    def sync_link(path, *, directory_fd=None):
        info = os.fstat(directory_fd)
        if (info.st_dev, info.st_ino) == (root_info.st_dev, root_info.st_ino):
            raise OSError(errno.EINVAL, "directory fsync unsupported")
        sync(path, directory_fd=directory_fd)

    monkeypatch.setattr(storage, "sync_directory", sync_link)
    if not new_link:
        (destination / source.name).write_bytes(source.path.read_bytes())
    repo = repository.EpisodeRepository(destination)
    if new_link:
        with pytest.raises(DownloadConflict, match="directory sync failed"):
            repo.download_episode(episode, "saved.mp3", _transfer)
    else:
        assert (
            repo.download_episode(episode, "saved.mp3", _transfer)
            == destination / "saved.mp3"
        )
        assert (destination / "saved.mp3").read_bytes() == b"completed audio"
