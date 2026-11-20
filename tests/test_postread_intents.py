"""A publication beginning during a read must be resolved before acceptance."""

import os
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from threading import Event
from types import SimpleNamespace

import pytest

from pdcst.repository import (
    AUDIO_INTENT,
    REFRESH_INTENT,
    EpisodeRepository,
    StoreReadError,
    clear_intent,
    write_intent,
)


@pytest.mark.parametrize("marker_name", [AUDIO_INTENT, REFRESH_INTENT])
@pytest.mark.parametrize("settled", [False, True])
@pytest.mark.parametrize("short_read", [False, True])
def test_postread_marker_fences_or_rereads(
    tmp_path, monkeypatch, episode, marker_name, settled, short_read
):
    writer = EpisodeRepository(tmp_path)
    writer.add(episode)
    reader = EpisodeRepository(tmp_path)
    read_started, published, observed = Event(), Event(), Event()
    marker = tmp_path / marker_name
    original_read = reader._read_bytes
    original_markers = reader._intent_markers
    calls = []

    real_fdopen = os.fdopen

    @contextmanager
    def open_short_generation(descriptor, *args, **kwargs):
        with real_fdopen(descriptor, *args, **kwargs) as source:
            yield SimpleNamespace(
                fileno=source.fileno, read=lambda limit: source.read(1)
            )

    def interrupted_read():
        calls.append(True)
        if len(calls) > 1:
            return original_read()
        try:
            with monkeypatch.context() as patch:
                if short_read:
                    patch.setattr(os, "fdopen", open_short_generation)
                return original_read()
        finally:
            read_started.set()
            assert published.wait(5)

    def observe_markers():
        markers = original_markers()
        if markers:
            observed.set()
        return markers

    def publish():
        assert read_started.wait(5)
        with writer._lock():
            locked = writer._fetch_all()
            write_intent(marker, {"kind": "synthetic"})
            locked[episode.guid].title = "Published title"
            writer._write_models(locked)
            published.set()
            assert observed.wait(5)
            if settled:
                clear_intent(marker)

    monkeypatch.setattr(reader, "_read_bytes", interrupted_read)
    monkeypatch.setattr(reader, "_intent_markers", observe_markers)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(publish)
        try:
            if settled:
                assert reader.list()[0].title == "Published title"
                assert len(calls) == 2
            else:
                with pytest.raises(
                    StoreReadError, match="unresolved publication.*README"
                ):
                    reader.list()
                assert marker.exists()
        finally:
            observed.set()
            future.result(5)
