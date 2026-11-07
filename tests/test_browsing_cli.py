"""Exercise registered browsing commands with real, isolated JSON repositories."""

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

import pdcst
from pdcst.models import Episode, Feed, Podcast
from pdcst.repository import EpisodeRepository, PodcastRepository

# Import in a fresh process so settings reads PDCST_HOME from the environment.
CLI_PROBE = """
import json
import sys
from pathlib import Path
from unittest.mock import patch
from contextlib import ExitStack

import httpx
import pdcst
from typer.testing import CliRunner
from pdcst.models import Episode
from pdcst.repository import EpisodeRepository, FeedParserRepository, JsonRepository

observed = []
opened = []
original_init = EpisodeRepository.__init__
original_str = Episode.__str__

def open_episodes(self, base_dir):
    assert sys.argv[1], 'Unexpected episode repository access'
    assert Path(base_dir).resolve() == Path(sys.argv[1]).resolve()
    opened.append(str(Path(base_dir).resolve()))
    original_init(self, base_dir)

def show_episode(self):
    observed.append({
        'title': self.title,
        'podcast': None if self.podcast is None else self.podcast.title,
    })
    return original_str(self)

with ExitStack() as stack:
    for owner, name in [
        (httpx.Client, 'send'),
        (FeedParserRepository, 'get'),
        (pdcst, 'download_with_progress'),
        (JsonRepository, '_lock'),
        (JsonRepository, '_write_models'),
    ]:
        stack.enter_context(patch.object(
            owner, name, side_effect=AssertionError('Browsing must be read-only/offline')
        ))
    stack.enter_context(patch.object(EpisodeRepository, '__init__', open_episodes))
    stack.enter_context(patch.object(Episode, '__str__', show_episode))
    result = CliRunner().invoke(pdcst.cli, sys.argv[2:], color=False)

print(json.dumps({
    'exit_code': result.exit_code,
    'output': result.output,
    'exception': repr(result.exception),
    'episodes': observed,
    'opened': opened,
}))
"""


def run_browse(root, args, episode_dir=None):
    def snapshot():
        return {
            str(path.relative_to(root)): path.read_bytes() if path.is_file() else None
            for path in root.rglob("*")
        }

    before = snapshot()
    env = dict(
        os.environ,
        PDCST_HOME=str(root),
        PYTHONPATH=str(Path(pdcst.__file__).resolve().parent.parent),
    )
    process = subprocess.run(
        [sys.executable, "-c", CLI_PROBE, str(episode_dir or ""), *args],
        cwd=root.parent,  # Neither the checkout nor the configured data root.
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert process.returncode == 0, process.stderr
    result = json.loads(process.stdout)
    assert result["exit_code"] == 0, result
    assert snapshot() == before
    return result


def make_podcast(title, directory, count):
    return Podcast(
        feed=Feed(
            url=f"https://example.invalid/{directory}/feed.xml",
            updated=datetime(2024, 1, 1, tzinfo=timezone.utc),
        ),
        title=title,
        directory=directory,
        episodes_count=count,
    )


def test_podcasts_empty_store(tmp_path):
    root = tmp_path / "subscriptions"
    root.mkdir()
    result = run_browse(root, ["podcasts"])
    assert result["output"] == ""
    assert result["opened"] == []


def test_podcasts_lists_all_subscriptions_from_configured_root(tmp_path):
    root = tmp_path / "subscriptions"
    root.mkdir()
    repository = PodcastRepository(root)
    repository.add_list(
        [
            make_podcast("Amber Observatory", "amber-store", 2),
            make_podcast("Birch Workshop", "birch-store", 7),
        ]
    )

    result = run_browse(root, ["podcasts"])

    assert result["output"].splitlines() == [
        "Amber Observatory - 2 episodes",
        "Birch Workshop - 7 episodes",
    ]
    assert result["opened"] == []


@pytest.fixture
def browsing_store(tmp_path):
    root = tmp_path / "subscriptions"
    root.mkdir()
    amber = make_podcast("Amber Observatory", "amber-store", 2)
    birch = make_podcast("Birch Workshop", "birch-store", 1)
    empty = make_podcast("Empty Studio", "empty-store", 0)
    PodcastRepository(root).add_list([amber, birch, empty])
    for podcast, titles in [
        (amber, ["First Light", "Second Orbit"]),
        (birch, ["Carving a Spoon"]),
        (empty, []),
    ]:
        EpisodeRepository(root / podcast.directory).add_list(
            [
                Episode(
                    podcast=podcast,
                    index=index,
                    guid=f"{podcast.directory}-{index}",
                    audio_url=f"https://example.invalid/{podcast.directory}/{index}.mp3",
                    published=datetime(2024, 1, index, tzinfo=timezone.utc),
                    title=title,
                )
                for index, title in enumerate(titles, start=1)
            ]
        )
    return root


@pytest.mark.parametrize(
    "identifier,directory,podcast,titles",
    [
        ("aMbEr", "amber-store", "Amber Observatory", ["First Light", "Second Orbit"]),
        ("WORK", "birch-store", "Birch Workshop", ["Carving a Spoon"]),
        ("eMpTy", "empty-store", "Empty Studio", []),
    ],
)
def test_episodes_lists_only_selected_show(
    browsing_store, identifier, directory, podcast, titles
):
    episode_dir = browsing_store / directory
    result = run_browse(browsing_store, ["episodes", identifier], episode_dir)

    assert result["output"].splitlines() == titles
    assert result["opened"] == [str(episode_dir.resolve())]
    assert result["episodes"] == [
        {"title": title, "podcast": podcast} for title in titles
    ]


def test_episodes_no_matching_show_is_silent(browsing_store):
    result = run_browse(browsing_store, ["episodes", "unknown-show"])
    assert result["output"] == ""
    assert result["opened"] == result["episodes"] == []


def test_episodes_empty_store_is_silent(tmp_path):
    root = tmp_path / "subscriptions"
    root.mkdir()
    result = run_browse(root, ["episodes", "amber"])
    assert result["output"] == ""
    assert result["opened"] == result["episodes"] == []
