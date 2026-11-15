"""Kernel claims release on owner death; successors validate current JSON."""

import copy
import json

import pytest

from pdcst.repository import EpisodeRepository
from tests.test_interrupted_publication import (
    paused_cli,
    wait_barrier,
)

DOWNLOAD = """
import sys
from contextlib import contextmanager
from pathlib import Path
import pdcst
from pdcst import repository
home = pdcst.settings.root
role = sys.argv[1]
lock = repository.advisory_lock
@contextmanager
def intercepted(path):
    if path.name.endswith('.download.lock') and role == 'successor':
        (home / 'claim-waiting').touch()
        with lock(path):
            (home / 'claim-acquired').touch()
            sys.stdin.readline()
            yield
    else:
        with lock(path):
            yield
repository.advisory_lock = intercepted
def transfer(url, stage):
    (home / (role + '-transfer')).write_text(url)
    if role == 'owner':
        (home / 'owner-ready').touch()
        sys.stdin.readline()
    stage.write_bytes(b'fresh synthetic audio')
pdcst.download_with_progress = transfer
pdcst.cli(['download', 'python', sys.argv[2]], standalone_mode=False)
"""


@pytest.mark.parametrize("change", ["enclosure", "target"])
def test_killed_transfer_releases_claim_and_successor_rechecks(
    publication_store, change
):
    home, episodes = publication_store
    target = episodes.base_dir / "saved.mp3"
    original = episodes.path.read_bytes()
    with paused_cli(home, DOWNLOAD, "owner", "refactor") as owner:
        wait_barrier(owner, home / "owner-ready")
        with paused_cli(home, DOWNLOAD, "successor", "refactor") as successor:
            wait_barrier(successor, home / "claim-waiting")
            # The owner still holds the actual kernel claim, not an injected lock.
            assert not (home / "claim-acquired").exists()
            assert episodes.path.read_bytes() == original
            owner.kill()
            owner.wait(timeout=5)
            wait_barrier(successor, home / "claim-acquired")
            # B has selected the old snapshot and now owns the download claim.
            # A normal metadata writer remains independent of this claim.
            repo = EpisodeRepository(episodes.base_dir)
            with repo._lock():
                rows = repo._fetch_all()
                current = next(iter(rows.values()))
                if change == "enclosure":
                    current.audio_url = "https://example.test/new.mp3"
                    current.audio_file = None
                else:
                    other = copy.deepcopy(current)
                    other.guid = "synthetic-other"
                    other.title = "Other episode"
                    other.audio_url = "https://example.test/other.mp3"
                    rows[other.guid] = other
                    current.audio_file = None
                    target.write_bytes(b"new owner audio")
                repo._write_models(rows)
            updated = repo.path.read_bytes()
            successor.stdin.write("continue\n")
            successor.stdin.flush()
            out, err = successor.communicate(timeout=10)
            assert successor.returncode != 0
            expected = "enclosure changed" if change == "enclosure" else "already owned"
            assert expected in out + err
            assert not (home / "successor-transfer").exists()
            assert repo.path.read_bytes() == updated
            assert target.read_bytes() == (
                b"previous audio" if change == "enclosure" else b"new owner audio"
            )
    # A killed transfer leaves its private stage, never a publication intent.
    (orphan,) = episodes.base_dir.glob(".pdcst-download-*")
    orphan_bytes = orphan.read_bytes()
    if change == "enclosure":
        # Changed enclosures deliberately retain the old unowned bytes; recover
        # them explicitly before the fresh selection can use the same filename.
        target.rename(episodes.base_dir / ".pdcst-download-manual")
    with paused_cli(
        home, DOWNLOAD, "retry", "refactor" if change == "enclosure" else "other"
    ) as retry:
        out, err = retry.communicate(timeout=10)
        assert retry.returncode == 0, out + err
    assert target.read_bytes() == b"fresh synthetic audio"
    assert (
        home / "retry-transfer"
    ).read_text() == f"https://example.test/{'new' if change == 'enclosure' else 'other'}.mp3"
    rows = json.loads(episodes.path.read_text())
    owner_row = next(row for row in rows if row["audio_file"] == "saved.mp3")
    assert owner_row["audio_url"] == (home / "retry-transfer").read_text()
    assert orphan.read_bytes() == orphan_bytes
    assert not list(home.rglob("*intent.json"))
