"""Synthetic callback barriers for audio publication admission."""

import os
from pathlib import Path

import pytest

from pdcst.repository import AUDIO_INTENT, DownloadConflict


@pytest.mark.parametrize("when", ["before", "during"])
@pytest.mark.parametrize("kind", ["directory", "fifo"])
def test_owned_destination_must_remain_regular(publication_store, episode, when, kind):
    _, repo = publication_store
    target = repo.base_dir / "saved.mp3"
    before = repo.path.read_bytes()
    calls = []

    def replace_target():
        target.unlink()
        if kind == "directory":
            target.mkdir()
            (target / "keep").write_bytes(b"unrelated entry")
        else:
            os.mkfifo(target)

    if when == "before":
        replace_target()
    identity = target.lstat() if when == "before" else None

    def transfer(url, stage, **kwargs):
        nonlocal identity
        calls.append(stage)
        stage.write_bytes(b"completed audio")
        replace_target()
        identity = target.lstat()

    with pytest.raises(DownloadConflict, match="regular file"):
        repo.download_episode(episode, target.name, transfer)
    assert len(calls) == (when == "during")
    assert target.lstat().st_ino == identity.st_ino
    if kind == "directory":
        assert (target / "keep").read_bytes() == b"unrelated entry"
    assert repo.path.read_bytes() == before
    assert not (repo.base_dir / AUDIO_INTENT).exists()
    assert not list(repo.base_dir.glob("*.previous"))
    if calls:
        assert calls[0].read_bytes() == b"completed audio"


@pytest.mark.parametrize(
    "kind", ["noop", "empty", "missing", "directory", "fifo", "symlink", "oversize"]
)
def test_transfer_result_admitted_before_intent(
    publication_store, episode, monkeypatch, kind
):
    from pdcst import repository

    _, repo = publication_store
    target = repo.base_dir / "saved.mp3"
    before = repo.path.read_bytes()
    identity = target.lstat()
    outside = repo.base_dir / "unrelated"
    outside.write_bytes(b"unrelated bytes")
    outside.chmod(0o640)
    monkeypatch.setattr(repository.settings, "audio_max_bytes", 16)

    def unexpected_intent(*args):
        pytest.fail("invalid transfer reached intent publication")

    monkeypatch.setattr(repository, "write_intent", unexpected_intent)
    stages = []

    def transfer(url, stage, **kwargs):
        stages.append(stage)
        if kind == "noop":
            return
        if kind == "empty":
            stage.write_bytes(b"")
            return
        stage.unlink()
        if kind == "directory":
            stage.mkdir()
            (stage / "keep").write_bytes(b"unrelated entry")
        elif kind == "fifo":
            os.mkfifo(stage)
        elif kind == "symlink":
            stage.symlink_to(outside)
        elif kind == "oversize":
            stage.write_bytes(b"x" * 17)

    with pytest.raises(DownloadConflict, match="stage"):
        repo.download_episode(episode, target.name, transfer)
    assert target.lstat() == identity
    assert target.read_bytes() == b"previous audio"
    assert repo.path.read_bytes() == before
    assert outside.read_bytes() == b"unrelated bytes"
    assert outside.stat().st_mode & 0o777 == 0o640
    assert not (repo.base_dir / AUDIO_INTENT).exists()
    assert not list(repo.base_dir.glob("*.previous"))
    if kind == "directory":
        assert (stages[0] / "keep").read_bytes() == b"unrelated entry"


@pytest.mark.parametrize("change", ["replace", "rewrite"])
def test_stage_generation_rechecked_before_intent(
    publication_store, episode, monkeypatch, change
):
    _, repo = publication_store
    target = repo.base_dir / "saved.mp3"
    before = repo.path.read_bytes()
    original = repo._download_record
    stages = []

    def transfer(url, stage, **kwargs):
        stages.append(stage)
        stage.write_bytes(b"completed audio")

    def change_after_admission(*args):
        result = original(*args)
        if stages:
            stage = stages[0]
            if change == "replace":
                stage.rename(stage.with_suffix(".retained"))
            stage.write_bytes(b"changed payload")
        return result

    monkeypatch.setattr(repo, "_download_record", change_after_admission)
    with pytest.raises(DownloadConflict, match="stage.*changed"):
        repo.download_episode(episode, target.name, transfer)
    assert target.read_bytes() == b"previous audio"
    assert repo.path.read_bytes() == before
    assert not (repo.base_dir / AUDIO_INTENT).exists()
    assert not list(repo.base_dir.glob("*.previous"))


@pytest.mark.parametrize("side", ["destination", "stage"])
@pytest.mark.parametrize("change", ["directory", "fifo", "replace", "rewrite"])
def test_publication_barrier_rechecks_generations(
    publication_store, episode, monkeypatch, side, change
):
    from pdcst import repository

    _, repo = publication_store
    target = repo.base_dir / "saved.mp3"
    before = repo.path.read_bytes()
    original = repository.write_intent
    changed_paths = []

    def transfer(url, stage, **kwargs):
        stage.write_bytes(b"completed audio")

    def change_at_intent(path, details, **kwargs):
        original(path, details, **kwargs)
        affected = target if side == "destination" else Path(details["stage"])
        if change != "rewrite":
            affected.rename(affected.with_suffix(".retained"))
        if change == "directory":
            affected.mkdir()
            (affected / "keep").write_bytes(b"unrelated entry")
        elif change == "fifo":
            os.mkfifo(affected)
        else:
            affected.write_bytes(b"changed payload")
        changed_paths.append((affected, affected.lstat().st_ino))

    monkeypatch.setattr(repository, "write_intent", change_at_intent)
    with pytest.raises(DownloadConflict, match="regular file|generation changed"):
        repo.download_episode(episode, target.name, transfer)
    affected, inode = changed_paths[0]
    assert affected.lstat().st_ino == inode
    if change == "directory":
        assert (affected / "keep").read_bytes() == b"unrelated entry"
    elif change in {"replace", "rewrite"}:
        assert affected.read_bytes() == b"changed payload"
    if side == "stage":
        assert target.read_bytes() == b"previous audio"
    assert repo.path.read_bytes() == before
    assert not (repo.base_dir / AUDIO_INTENT).exists()
    assert not list(repo.base_dir.glob("*.previous"))


@pytest.mark.parametrize("owned", [False, True])
def test_exact_limit_stage_publishes_once(
    publication_store, episode, monkeypatch, owned
):
    from pdcst import repository

    _, repo = publication_store
    target = repo.base_dir / "saved.mp3"
    if not owned:
        target.unlink()
        episode.audio_file = None
        repo.add(episode)
    else:
        target.chmod(0o640)
    monkeypatch.setattr(repository.settings, "audio_max_bytes", 16)
    calls = []

    def transfer(url, stage, **kwargs):
        calls.append(stage)
        stage.write_bytes(b"a" * 16)

    assert repo.download_episode(episode, target.name, transfer) == target
    assert len(calls) == 1
    assert target.read_bytes() == b"a" * 16
    assert repo.all[episode.guid].audio_file == target.name
    if owned:
        assert target.stat().st_mode & 0o777 == 0o640
    assert not list(repo.base_dir.glob(".pdcst-download-*"))


def test_stage_change_while_moving_previous_audio_restores_it(
    publication_store, episode, monkeypatch
):
    from pdcst import repository

    _, repo = publication_store
    target = repo.base_dir / "saved.mp3"
    before = repo.path.read_bytes()
    replace = repository.os.replace
    stages = []

    def transfer(url, stage, **kwargs):
        stages.append(stage)
        stage.write_bytes(b"completed audio")

    def change_after_backup(source, destination, **kwargs):
        result = replace(source, destination, **kwargs)
        if Path(source).name == target.name:
            stages[0].write_bytes(b"changed payload")
        return result

    monkeypatch.setattr(repository.os, "replace", change_after_backup)
    with pytest.raises(DownloadConflict, match="stage generation changed.*inspect"):
        repo.download_episode(episode, target.name, transfer)
    assert target.read_bytes() == b"previous audio"
    assert stages[0].read_bytes() == b"changed payload"
    assert repo.path.read_bytes() == before
    assert not (repo.base_dir / AUDIO_INTENT).exists()
    assert not list(repo.base_dir.glob("*.previous"))
