import copy
import json
import os
from datetime import timedelta
from pathlib import Path

import pytest

import pdcst
from pdcst.repository import EpisodeRepository, PodcastRepository, StoreReadError


def test_refresh_preserves_valid_download(tmp_path, monkeypatch, podcast, episode):
    monkeypatch.setattr(pdcst.settings, "root", tmp_path)
    directory = tmp_path / podcast.directory
    directory.mkdir()
    (directory / "saved.mp3").write_bytes(b"synthetic audio")
    episode.audio_file = "saved.mp3"
    repo = EpisodeRepository(directory)
    repo.add(episode)
    # Read once: importing must still reload under the lock.
    repo.list()
    fresh = copy.deepcopy(episode)
    fresh.audio_file = None
    fresh.title = "Changed title"
    fresh.published += timedelta(days=1)
    new = copy.deepcopy(fresh)
    new.guid = "new"

    class Parser:
        def get(self, *_):
            return podcast, [fresh, new]

    pdcst.subscribe(
        podcast.feed.url, "{title}", None, Parser(), PodcastRepository(tmp_path)
    )
    saved, added = EpisodeRepository(directory).list()
    assert saved.audio_file == "saved.mp3"
    assert saved.title == fresh.title and saved.published == fresh.published
    assert added.audio_file is None
    saved.audio_file = None
    repo.add(saved)
    assert repo.list()[0].audio_file is None


@pytest.mark.parametrize("field", ["guid", "audio_url", "url"])
@pytest.mark.parametrize("invalid", [123, ""])
def test_refresh_refuses_invalid_stored_identity(
    tmp_path, monkeypatch, podcast, episode, field, invalid
):
    monkeypatch.setattr(pdcst.settings, "root", tmp_path)
    podcasts = PodcastRepository(tmp_path)
    episodes = EpisodeRepository(tmp_path / podcast.directory)
    episode.guid = "123"
    podcasts.add(podcast)
    episodes.add(episode)
    assert episodes.list()[0].guid == "123"

    path = podcasts.path if field == "url" else episodes.path
    rows = json.loads(path.read_bytes())
    fields = rows[0]["feed"] if field == "url" else rows[0]
    fields[field] = invalid
    path.write_text(json.dumps(rows))
    before = {path: path.read_bytes() for path in (podcasts.path, episodes.path)}

    class Parser:
        def get(self, *_):
            return podcast, [episode]

    with pytest.raises(StoreReadError, match="invalid schema"):
        pdcst.subscribe(podcast.feed.url, "{title}", None, Parser(), podcasts)
    assert {path: path.read_bytes() for path in before} == before


def test_refresh_drops_audio_replaced_by_symlink(
    tmp_path, monkeypatch, podcast, episode
):
    monkeypatch.setattr(pdcst.settings, "root", tmp_path)
    podcasts = PodcastRepository(tmp_path)
    podcasts.add(podcast)
    episodes = EpisodeRepository(tmp_path / podcast.directory)
    episode.audio_file = "saved.mp3"
    episodes.add(episode)
    saved = episodes.base_dir / episode.audio_file
    saved.write_bytes(b"previous audio")
    other = episodes.base_dir / "other.mp3"
    other.write_bytes(b"unrelated audio")

    original_stat = os.stat
    swapped = False

    def swap_before_stat(path, *, dir_fd=None, follow_symlinks=True):
        nonlocal swapped
        # Interleave just before the retention stat, after any separate path
        # checks. The path-based and descriptor-based forms name the same entry.
        if not swapped and (
            (path == saved and follow_symlinks)
            or (path == saved.name and dir_fd is not None)
        ):
            saved.unlink()
            saved.symlink_to(other.name)
            swapped = True
        return original_stat(path, dir_fd=dir_fd, follow_symlinks=follow_symlinks)

    class Parser:
        def get(self, *_):
            return podcast, [episode]

    monkeypatch.setattr(os, "stat", swap_before_stat)
    pdcst.subscribe(podcast.feed.url, "{title}", None, Parser(), podcasts)

    assert swapped
    assert episodes.list()[0].audio_file is None
    assert saved.readlink() == Path(other.name)
    assert other.read_bytes() == b"unrelated audio"


@pytest.mark.parametrize(
    "name",
    ["missing.mp3", "../outside.mp3", "/outside.mp3", "link.mp3", "episodes.json"],
)
def test_import_drops_invalid_association(tmp_path, episode, name):
    (tmp_path / "link.mp3").symlink_to(tmp_path.parent / "outside.mp3")
    repo = EpisodeRepository(tmp_path)
    episode.audio_file = name
    repo.add(episode)
    episode.audio_file = None
    repo.import_list([episode])
    assert repo.list()[0].audio_file is None


@pytest.mark.parametrize(
    "titles", [("Same Show", "Same Show"), ("Same/Show", "Same:Show")]
)
def test_distinct_feeds_have_distinct_directories(tmp_path, monkeypatch, feed, titles):
    from pdcst.models import Podcast

    monkeypatch.setattr(pdcst.settings, "root", tmp_path)

    class Parser:
        def get(self, url, directory, pattern):
            current = copy.deepcopy(feed)
            current.url = url
            return (
                Podcast(
                    feed=current,
                    title=titles[int(url[-1])],
                    episodes_count=0,
                    directory=directory,
                ),
                [],
            )

    repo = PodcastRepository(tmp_path)
    a = pdcst.subscribe("https://example.test/0", "{title}", None, Parser(), repo)
    b = pdcst.subscribe("https://example.test/1", "{title}", None, Parser(), repo)
    assert a.directory != b.directory
    refreshed = pdcst.subscribe(b.feed.url, "{title}", None, Parser(), repo)
    assert refreshed.directory == b.directory
    before = {p: p.read_bytes() for p in tmp_path.rglob("*.json")}
    with pytest.raises(ValueError, match="already used"):
        pdcst.subscribe(b.feed.url, "{title}", a.directory, Parser(), repo)
    assert before == {p: p.read_bytes() for p in tmp_path.rglob("*.json")}


def test_collision_directory_resolutions_are_linear(tmp_path, monkeypatch, podcast):
    monkeypatch.setattr(pdcst.settings, "root", tmp_path)
    repo = PodcastRepository(tmp_path)
    count = 1000
    subscriptions = []
    for number in range(1, count + 1):
        stored = copy.deepcopy(podcast)
        stored.feed.url = f"https://example.test/{number}"
        stored.directory = "PYTHON_PODCAST" + (f"_{number}" if number > 1 else "")
        subscriptions.append(stored)
    repo.add_list(subscriptions)
    calls = 0
    original = pdcst.contained_path

    def counted(*args):
        nonlocal calls
        calls += 1
        # Fail early on the quadratic implementation without a slow test.
        assert calls <= 2 * count + 5
        return original(*args)

    class Parser:
        def get(self, *_):
            return podcast, []

    monkeypatch.setattr(pdcst, "contained_path", counted)
    result = pdcst.subscribe(podcast.feed.url, "{title}", None, Parser(), repo)
    assert result.directory == f"python_podcast_{count + 1}"


@pytest.mark.parametrize("unsafe", ["../outside", "/absolute", "escaping-link"])
def test_unsafe_legacy_subscription_does_not_block_others(
    tmp_path, monkeypatch, podcast, unsafe
):
    monkeypatch.setattr(pdcst.settings, "root", tmp_path)
    (tmp_path / "escaping-link").symlink_to(tmp_path.parent, target_is_directory=True)
    repo = PodcastRepository(tmp_path)
    bad = copy.deepcopy(podcast)
    bad.feed.url = "https://example.test/unsafe"
    bad.directory = unsafe
    repo.add(bad)

    class Parser:
        def get(self, *_):
            return podcast, []

    result = pdcst.subscribe(podcast.feed.url, "{title}", None, Parser(), repo)
    assert result.directory == podcast.directory
    assert len(repo.list()) == 2


def test_shared_legacy_directory_requires_explicit_separation(
    tmp_path, monkeypatch, podcast
):
    monkeypatch.setattr(pdcst.settings, "root", tmp_path)
    repo = PodcastRepository(tmp_path)
    other = copy.deepcopy(podcast)
    other.feed.url = "https://example.test/other"
    repo.add_list([podcast, other])
    before = repo.path.read_bytes()

    class Parser:
        def get(self, _url, directory, _pattern):
            fresh = copy.deepcopy(podcast)
            if directory is not None:
                fresh.directory = directory
            return fresh, []

    with pytest.raises(ValueError, match="choose a distinct --podcast-dir"):
        pdcst.subscribe(podcast.feed.url, "{title}", None, Parser(), repo)
    assert repo.path.read_bytes() == before
    assert not (tmp_path / podcast.directory).exists()
    result = pdcst.subscribe(podcast.feed.url, "{title}", "separated", Parser(), repo)
    assert result.directory == "separated"
    assert (
        next(p for p in repo.list() if p.feed.url == other.feed.url).directory
        == other.directory
    )


@pytest.mark.parametrize("existing", [False, True])
@pytest.mark.parametrize("failure", ["corrupt", "read", "replace"])
def test_episode_import_failure_keeps_subscription(
    tmp_path, monkeypatch, podcast, episode, existing, failure
):
    monkeypatch.setattr(pdcst.settings, "root", tmp_path)
    repo = PodcastRepository(tmp_path)
    if existing:
        repo.add(podcast)
    before = repo.path.read_bytes() if existing else None
    original_subscription = repo.list()  # Read before the attempted import.
    fresh = copy.deepcopy(podcast)
    fresh.directory = "updated"
    fresh.episodes_count = 9
    directory = tmp_path / fresh.directory
    directory.mkdir()
    episodes = EpisodeRepository(directory)
    episodes.path.write_text("broken" if failure == "corrupt" else "[]")
    episode_bytes = episodes.path.read_bytes()
    injected = []

    class Parser:
        def get(self, *_):
            return fresh, [episode]

    def fail_read(self):
        injected.append("read")
        raise OSError("injected read")

    if failure == "read":
        monkeypatch.setattr(EpisodeRepository, "_fetch_all", fail_read)
    elif failure == "replace":
        from pdcst.storage import os

        replace = os.replace

        def fail_replace(source, target, **kwargs):
            if Path(target).name == episodes.path.name:
                injected.append("replace")
                raise OSError("injected replace")
            return replace(source, target, **kwargs)

        monkeypatch.setattr(os, "replace", fail_replace)

    with pytest.raises((ValueError, OSError)):
        pdcst.subscribe(podcast.feed.url, "{title}", fresh.directory, Parser(), repo)
    if failure != "corrupt":
        assert injected == [failure]
    assert (repo.path.read_bytes() if repo.path.exists() else None) == before
    assert repo.list() == original_subscription
    assert episodes.path.read_bytes() == episode_bytes
    assert not list(tmp_path.rglob("*.tmp"))


@pytest.mark.parametrize("changed_enclosure", [False, True])
def test_subscription_publish_failure_is_retryable(
    tmp_path, monkeypatch, podcast, episode, changed_enclosure
):
    from pdcst.storage import os

    monkeypatch.setattr(pdcst.settings, "root", tmp_path)
    repo = PodcastRepository(tmp_path)
    repo.add(podcast)
    before = repo.path.read_bytes()
    directory = tmp_path / podcast.directory
    directory.mkdir()
    (directory / "saved.mp3").write_bytes(b"synthetic")
    episode.audio_file = "saved.mp3"
    episodes = EpisodeRepository(directory)
    episodes.add(episode)
    fresh = copy.deepcopy(podcast)
    fresh.episodes_count = 2
    incoming = copy.deepcopy(episode)
    incoming.audio_file = None
    incoming.title = "Updated"
    if changed_enclosure:
        incoming.audio_url = "https://example.test/revised.mp3"
    episode_before = episodes.path.read_bytes()

    class Parser:
        def get(self, *_):
            return fresh, [incoming]

    replace = os.replace
    injected = []

    def fail_publish(source, target, **kwargs):
        if Path(target).name == repo.path.name:
            injected.append(True)
            raise OSError("injected publication")
        return replace(source, target, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(os, "replace", fail_publish)
        with pytest.raises(OSError, match="injected publication"):
            pdcst.subscribe(podcast.feed.url, "{title}", None, Parser(), repo)
    assert injected == [True]
    assert repo.path.read_bytes() == before
    imported = EpisodeRepository(directory).list()[0]
    assert imported.title == episode.title
    assert episodes.path.read_bytes() == episode_before
    assert (directory / "saved.mp3").read_bytes() == b"synthetic"
    assert imported.audio_file == "saved.mp3"
    pdcst.subscribe(podcast.feed.url, "{title}", None, Parser(), repo)
    assert repo.list()[0].episodes_count == 1
    assert EpisodeRepository(directory).list()[0].audio_file == (
        None if changed_enclosure else "saved.mp3"
    )


@pytest.mark.parametrize("link", ["internal", "loop"])
def test_refresh_drops_symlink_but_keeps_real_owner(tmp_path, episode, link):
    repo = EpisodeRepository(tmp_path)
    target = tmp_path / "real.mp3"
    target.write_bytes(b"synthetic audio")
    alias = tmp_path / "alias.mp3"
    alias.symlink_to(target if link == "internal" else alias)
    owner = copy.deepcopy(episode)
    owner.audio_file = target.name
    linked = copy.deepcopy(episode)
    linked.guid = "linked"
    linked.audio_file = alias.name
    repo.add_list([owner, linked])
    incoming = [copy.deepcopy(owner), copy.deepcopy(linked)]
    for row in incoming:
        row.audio_file = None
    repo.import_list(incoming)
    saved = repo.all
    assert saved[owner.guid].audio_file == target.name
    assert saved[linked.guid].audio_file is None
    assert target.read_bytes() == b"synthetic audio"
    assert alias.is_symlink()


@pytest.mark.parametrize("refresh_both", [False, True])
def test_refresh_drops_shared_audio_ownership(tmp_path, episode, refresh_both):
    repo = EpisodeRepository(tmp_path)
    target = tmp_path / "shared.mp3"
    target.write_bytes(b"synthetic audio")
    episode.audio_file = target.name
    other = copy.deepcopy(episode)
    other.guid = "other"
    repo.add_list([episode, other])
    incoming = [episode, other] if refresh_both else [episode]
    repo.import_list(incoming)
    saved = repo.all
    assert saved[episode.guid].audio_file is None
    assert saved[other.guid].audio_file is None
    assert target.read_bytes() == b"synthetic audio"


@pytest.mark.parametrize("alias_kind", ["filename", "unicode", "inode"])
def test_refresh_drops_file_aliases_on_every_host(
    tmp_path, monkeypatch, episode, alias_kind
):
    from types import SimpleNamespace

    first = tmp_path / ("café.mp3" if alias_kind == "unicode" else "saved.mp3")
    # Emulate case-insensitive lookup or hard-link identity without relying on
    # host filename comparison or hard-link support.
    second = (
        tmp_path
        / {"filename": "SAVED.MP3", "unicode": "cafe\u0301.mp3", "inode": "alias.mp3"}[
            alias_kind
        ]
    )
    first.write_bytes(b"synthetic audio")
    if alias_kind == "inode":
        second.write_bytes(b"other synthetic audio")
    info = first.stat()
    original_stat = os.stat

    def alias_stat(path, *, dir_fd=None, follow_symlinks=True):
        if not follow_symlinks and path in (first, second):
            return SimpleNamespace(
                st_mode=info.st_mode,
                st_dev=info.st_dev,
                st_ino=(
                    info.st_ino
                    if alias_kind == "inode" or path == first
                    else info.st_ino + 1
                ),
            )
        return original_stat(path, dir_fd=dir_fd, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(os, "stat", alias_stat)
    episode.audio_file = first.name
    other = copy.deepcopy(episode)
    other.guid = "other"
    other.audio_file = second.name
    repo = EpisodeRepository(tmp_path)
    repo.add_list([episode, other])
    repo.import_list([episode, other])
    assert all(row.audio_file is None for row in repo.all.values())
    assert first.read_bytes() == b"synthetic audio"
    if alias_kind == "inode":
        assert second.read_bytes() == b"other synthetic audio"


@pytest.mark.parametrize("explicit", [False, True])
def test_unicode_directory_owners_refused_before_import(
    tmp_path, monkeypatch, podcast, explicit
):
    monkeypatch.setattr(pdcst.settings, "root", tmp_path)
    repo = PodcastRepository(tmp_path)
    podcast.directory = "café"
    other = copy.deepcopy(podcast)
    other.feed.url = "https://example.test/other"
    other.directory = "cafe\u0301"
    repo.add_list([podcast, other])
    before = repo.path.read_bytes()

    class Parser:
        def get(self, *_):
            return other, []

    with pytest.raises(ValueError, match="already used"):
        pdcst.subscribe(
            other.feed.url,
            "{title}",
            other.directory if explicit else None,
            Parser(),
            repo,
        )
    assert repo.path.read_bytes() == before
    assert not list(tmp_path.rglob("episodes.json"))


def test_refresh_compensation_failure_retains_both_generations(
    tmp_path, monkeypatch, podcast, episode
):
    from pdcst.storage import os

    monkeypatch.setattr(pdcst.settings, "root", tmp_path)
    repo = PodcastRepository(tmp_path)
    repo.add(podcast)
    episodes = EpisodeRepository(tmp_path / podcast.directory)
    episodes.add(episode)
    before = episodes.path.read_bytes()
    fresh = copy.deepcopy(episode)
    fresh.title = "Refreshed title"

    class Parser:
        def get(self, *_):
            return podcast, [fresh]

    replace = os.replace
    publications = []

    def fail(source, target, **kwargs):
        if Path(target).name == repo.path.name:
            raise OSError("subscription failed")
        if Path(target).name == episodes.path.name:
            publications.append(True)
            if len(publications) == 2:
                raise OSError("compensation failed")
        return replace(source, target, **kwargs)

    monkeypatch.setattr(os, "replace", fail)
    with pytest.warns(RuntimeWarning, match="Refresh rollback incomplete"):
        with pytest.raises(OSError, match="subscription failed"):
            pdcst.subscribe(podcast.feed.url, "{title}", None, Parser(), repo)
    assert episodes.path.read_bytes() != before
    retained = list(episodes.base_dir.glob(".pdcst-refresh-*"))
    assert any(p.read_bytes() == before for p in retained)
    assert any(b"Refreshed title" in p.read_bytes() for p in retained)


@pytest.mark.parametrize("existing_episodes", [False, True])
def test_compensation_holds_episode_lock_against_second_process(
    tmp_path, monkeypatch, podcast, episode, existing_episodes
):
    import os
    import select
    import subprocess
    import sys
    from pathlib import Path

    monkeypatch.setattr(pdcst.settings, "root", tmp_path)
    podcasts = PodcastRepository(tmp_path)
    podcasts.add(podcast)
    episodes = EpisodeRepository(tmp_path / podcast.directory)
    if existing_episodes:
        episodes.add(episode)
    incoming = copy.deepcopy(episode)
    incoming.title = "Unpublished refresh"
    concurrent = episodes.storage_model_type(**episode.dict())
    concurrent.guid = "concurrent writer"
    child = None

    class Parser:
        def get(self, *_):
            return podcast, [incoming]

    def fail_publication(_models):
        nonlocal child
        child = subprocess.Popen(
            [
                sys.executable,
                "-c",
                """
import sys
from pdcst.repository import EpisodeRepository, EpisodeInDb
print("started", flush=True)
EpisodeRepository(sys.argv[1]).add(EpisodeInDb.parse_raw(sys.argv[2]))
print("finished", flush=True)
""",
                str(episodes.base_dir),
                concurrent.json(),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=dict(os.environ, PYTHONPATH=str(Path(pdcst.__file__).parent.parent)),
        )
        assert select.select([child.stdout], [], [], 5)[0]
        assert child.stdout.readline().strip() == "started"
        assert not select.select([child.stdout], [], [], 0.3)[0]
        raise OSError("subscription publication failed")

    monkeypatch.setattr(podcasts, "_write_models", fail_publication)
    try:
        with pytest.raises(OSError, match="subscription publication failed"):
            pdcst.subscribe(podcast.feed.url, "{title}", None, Parser(), podcasts)
        out, err = child.communicate(timeout=5)
        assert child.returncode == 0, err
        assert out.strip() == "finished"
    finally:
        if child is not None and child.poll() is None:
            child.kill()
            child.communicate()
    saved = episodes.all
    assert saved[concurrent.guid].title == concurrent.title
    if existing_episodes:
        assert saved[episode.guid].title == episode.title
    else:
        assert episode.guid not in saved
    assert not list(episodes.base_dir.glob(".pdcst-refresh-*"))


@pytest.mark.parametrize("suffix", ["previous", "refreshed"])
def test_refresh_recovery_metadata_never_becomes_audio(tmp_path, episode, suffix):
    repo = EpisodeRepository(tmp_path)
    episode.audio_file = f".pdcst-refresh-retained.{suffix}"
    recovery = tmp_path / episode.audio_file
    recovery.write_bytes(b"retained metadata generation")
    repo.add(episode)
    repo.import_list([episode])
    assert repo.list()[0].audio_file is None
    assert recovery.read_bytes() == b"retained metadata generation"


def test_refresh_validates_one_locked_subscription_generation(
    tmp_path, monkeypatch, podcast, episode
):
    from pdcst import repository

    monkeypatch.setattr(pdcst.settings, "root", tmp_path)
    shows = PodcastRepository(tmp_path)
    podcast.title = "Synthetic Show!"
    podcast.directory = None
    shows.add(podcast)
    legacy = tmp_path / "synthetic_show!"
    legacy.mkdir()
    counts = {"read": 0, "validate": 0}
    read = PodcastRepository._read_bytes
    parse = repository.parse_obj_as

    def count_read(self):
        assert self._lock_depth == 1
        counts["read"] += 1
        return read(self)

    def count_parse(model_type, values):
        if model_type == list[shows.storage_model_type]:
            counts["validate"] += 1
        return parse(model_type, values)

    class Parser:
        def get(self, *_):
            fresh = copy.deepcopy(podcast)
            fresh.title = "Renamed Synthetic Show"
            return fresh, [episode]

    with monkeypatch.context() as patch:
        patch.setattr(PodcastRepository, "_read_bytes", count_read)
        patch.setattr(repository, "parse_obj_as", count_parse)
        refreshed = pdcst.subscribe(
            podcast.feed.url, "{index}.mp3", None, Parser(), shows
        )

    # The second raw read copies the original bytes into the recovery backup.
    assert counts == {"read": 2, "validate": 1}
    assert shows.all[podcast.feed.url].directory == refreshed.directory == legacy.name
    assert EpisodeRepository(legacy).list()[0].guid == episode.guid


def test_refresh_reconciles_audio_for_episodes_outside_feed_window(
    tmp_path, monkeypatch, podcast, episode
):
    monkeypatch.setattr(pdcst.settings, "root", tmp_path)
    shows = PodcastRepository(tmp_path)
    shows.add(podcast)
    episodes = EpisodeRepository(tmp_path / podcast.directory)
    missing = copy.deepcopy(episode)
    missing.guid = "expired-missing"
    missing.audio_file = "gone.mp3"
    valid = copy.deepcopy(episode)
    valid.guid = "expired-valid"
    valid.index = missing.index + 1
    valid.audio_file = "saved.mp3"
    episode.index = valid.index + 1
    episodes.add_list([missing, valid, episode])
    audio = episodes.base_dir / valid.audio_file
    audio.write_bytes(b"synthetic audio")
    before = episodes.all

    class Parser:
        def get(self, *_):
            return podcast, [episode]

    result = pdcst.subscribe(podcast.feed.url, "{index}.mp3", None, Parser(), shows)

    after = episodes.all
    assert result.episodes_count == len(after) == 3
    expected_missing = before[missing.guid].dict() | {"audio_file": None}
    assert after[missing.guid].dict() == expected_missing
    assert after[valid.guid].dict() == before[valid.guid].dict()
    assert audio.read_bytes() == b"synthetic audio"
