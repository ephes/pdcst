"""Only the documented filename fields can be formatted."""

import pytest

from pdcst.models import FilenamePatternError


@pytest.mark.parametrize(
    "pattern",
    [
        "{missing}",
        "{title.__class__}",
        "{title[0]}",
        "{index:{title}}",
        "{title!r}",
        "{",
        "{title:03d}",
        "{index:bad}",
        "../{title}.mp3",
        "x\x00.mp3",
    ],
)
def test_pattern_errors_are_bounded(podcast, episode, pattern):
    podcast.file_name_pattern = pattern
    with pytest.raises(ValueError, match="Invalid filename pattern") as error:
        podcast.get_audio_file_name(episode)
    assert len(str(error.value)) < 150


def test_padded_index_and_escaped_braces(podcast, episode):
    podcast.file_name_pattern = "{{show}}_{index:03}_{title}.{file_format}"
    assert podcast.get_audio_file_name(episode) == "{show}_001_refactoring.mp3"


@pytest.mark.parametrize(
    "pattern",
    [
        "{title:.3}.mp3",
        "nUl .mp3",
        "LPT².archive.mp3",
        "CONIN$.mp3",
        "episode.mp3.",
        "episode.mp3 ",
        "episode.mp3:stream",
        "episode\t.mp3",
    ],
)
def test_windows_incompatible_basename_is_refused(podcast, episode, pattern):
    # Precision can turn a title into a device name after formatting.
    episode.title = "CONcert"
    podcast.file_name_pattern = pattern
    with pytest.raises(FilenamePatternError, match="Invalid filename pattern"):
        podcast.get_audio_file_name(episode)


def test_ordinary_dotted_basename_is_portable(podcast, episode):
    podcast.file_name_pattern = "com10.{title}.archive.mp3"
    assert podcast.get_audio_file_name(episode) == "com10.refactoring.archive.mp3"


@pytest.mark.parametrize("locale_separator", [",", "."])
def test_locale_sensitive_index_has_migration_error(
    podcast, episode, monkeypatch, locale_separator
):
    locale_calls = []

    def simulated_format(value, spec):
        if spec.endswith("n"):
            locale_calls.append(spec)
            return format(value, ",d").replace(",", locale_separator)
        return format(value, spec)

    monkeypatch.setattr("pdcst.models.format", simulated_format, raising=False)
    episode.index = 12345
    podcast.file_name_pattern = "{index:04n}.mp3"
    with pytest.raises(FilenamePatternError, match="replace.*n.*with.*d"):
        podcast.get_audio_file_name(episode)
    with pytest.raises(FilenamePatternError, match="replace.*n.*with.*d"):
        podcast.validate_audio_filename_pattern()
    # The refusal happens before any locale-dependent formatting is attempted.
    assert locale_calls == []

    podcast.file_name_pattern = "{index:,d}_{index:_d}.mp3"
    assert podcast.get_audio_file_name(episode) == "12,345_12_345.mp3"


def test_filename_resolves_only_referenced_fields_once(podcast, episode, monkeypatch):
    calls = []
    sanitize = podcast.title_for_filename

    def record(value):
        calls.append(value)
        return sanitize(value)

    monkeypatch.setattr(podcast, "title_for_filename", record)
    episode.title = "Synthetic Long Title " * 4_096
    episode.audio_url = "https://example.test/download.CUSTOM?"
    episode.audio_media_type = None
    podcast.file_name_pattern = "{{title}}_{index:03}.mp3"
    assert podcast.get_audio_file_name(episode) == "{title}_001.mp3"
    assert calls == []

    podcast.file_name_pattern = "{title:.9}_{title:.4}.{{file_format}}"
    assert podcast.get_audio_file_name(episode) == "synthetic_synt.{file_format}"
    assert calls == [episode.title]

    calls.clear()
    podcast.file_name_pattern = "{index}.{file_format:.3}.{file_format}"
    assert podcast.get_audio_file_name(episode) == "1.cus.custom"
    assert calls == ["CUSTOM"]


@pytest.mark.parametrize(
    "pattern",
    [
        "{title:1000000000}",
        "{title:.1000000000}",
        "{index:01000000000}",
        "{title:256}",
        "{title:.256}",
        "{index:0256d}",
    ],
)
def test_expansion_rejected_before_formatting(podcast, episode, pattern, monkeypatch):
    def forbidden(*args):
        pytest.fail("unbounded spec reached field formatting")

    monkeypatch.setattr("pdcst.models.format", forbidden, raising=False)

    class GuardedPattern(str):
        def format(self, **kwargs):
            pytest.fail("unbounded pattern reached str.format")

    podcast.file_name_pattern = GuardedPattern(pattern)
    with pytest.raises(FilenamePatternError):
        podcast.get_audio_file_name(episode)


@pytest.mark.parametrize(
    "pattern,size",
    [("{index:0255d}", 255), ("{title:_<255}", 255), ("{title:.255}", 11)],
)
def test_format_limit_boundary(podcast, episode, pattern, size):
    podcast.file_name_pattern = pattern
    assert len(podcast.get_audio_file_name(episode).encode("utf-8")) == size


@pytest.mark.parametrize(
    "pattern,title",
    [
        ("x" * 256, "short"),
        ("{title}{title}", "a" * 128),
        ("{title}", "é" * 128),
        ("{title:.128}", "é" * 256),
    ],
)
def test_basename_byte_budget(podcast, episode, pattern, title):
    podcast.file_name_pattern = pattern
    episode.title = title
    with pytest.raises(FilenamePatternError):
        podcast.get_audio_file_name(episode)


@pytest.mark.parametrize(
    "pattern,expected",
    [
        ("{index:>3}_{title}", "  1_refactoring"),
        ("{title:_<14}", "refactoring___"),
        ("{index:,}", "1"),
        ("{index:+}", "+1"),
        ("{index:x}", "1"),
        ("{index:0>3}", "001"),
        ("{title:-^12.4}", "----refa----"),
    ],
)
def test_existing_fill_alignment_and_integer_specs_keep_working(
    podcast, episode, pattern, expected
):
    # Stored subscriptions may use standard format syntax; only numbers are bounded.
    podcast.file_name_pattern = pattern
    assert podcast.get_audio_file_name(episode) == expected


@pytest.mark.parametrize(
    "pattern",
    ["{title:05}", "{index:c}", "{title:_<256}", "{index:_>256}", "{title:.5d}"],
)
def test_ambiguous_or_unbounded_specs_are_refused(podcast, episode, pattern):
    podcast.file_name_pattern = pattern
    with pytest.raises(FilenamePatternError):
        podcast.get_audio_file_name(episode)


def test_overlong_pattern_names_its_own_limit(podcast, episode):
    podcast.file_name_pattern = "x" * 4097
    with pytest.raises(FilenamePatternError, match="4096 characters"):
        podcast.get_audio_file_name(episode)
