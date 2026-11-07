# pdcst

A small command line client for downloading podcast episodes.
It's in pre alpha status (just started a week ago).

# Installation

Python 3.10 or newer is required. The current storage/settings APIs use Pydantic 1.

```shell
$ pipx install pdcst
```

# Usage

## help

```shell
Usage: pdcst [OPTIONS] COMMAND [ARGS]...

Options:
  --install-completion [bash|zsh|fish|powershell|pwsh]
                                  Install completion for the specified
                                  shell.
  --show-completion [bash|zsh|fish|powershell|pwsh]
                                  Show completion for the specified shell,
                                  to copy it or customize the installation.
  --help                          Show this message and exit.

Commands:
  add       Subscribe to podcast with given FEED_URL.
  download  Download a podcast episode.
  episodes  List the available episodes for a podcast.
  podcasts  List your podcast subscriptions.
```

## Subsribe

To add a feed subscription call:

```shell
$ pdcst add https://python-podcast.de/show/feed/podcast/mp3/rss.xml --name-pattern "gpp_{index:03}_{title}.{file_format}"
```

Feeds must have a channel title and at least one usable audio episode (an ID,
title, publication date, and audio enclosure). The stored feed update date uses
the first valid value from channel update date (`lastBuildDate`/Atom `updated`),
channel publication date (`pubDate`), HTTP `Last-Modified`, then the newest usable
episode publication date. Previously only HTTP `Last-Modified` was used; it is
now preserved as a fallback rather than required. Empty or unusable feeds raise
`pdcst.repository.FeedError` (a `ValueError`) with a short diagnostic message.

Import chooses the first valid HTTP(S) enclosure whose declared media type is
supported: audio/mpeg, audio/mp3, audio/mp4, audio/aac, audio/x-m4a, audio/ogg,
audio/opus, audio/wav, audio/x-wav, audio/flac, or audio/x-flac. If none matches,
an enclosure without a media type may be used when its URL path ends in .mp3,
.m4a, .aac, .ogg, .oga, .opus, .wav, or .flac (case insensitive; query strings
are ignored). Explicit types outside the supported list are rejected, even with
an audio extension. Enclosure URLs containing whitespace are rejected.
Unusable entries are skipped with a warning reporting their count; valid sibling
episodes remain available and receive contiguous chronological indices.

Feed and episode timestamps are timezone-aware UTC and serialize with `+00:00`.
Existing stored timestamps without a timezone are read as UTC, the legacy
convention; explicitly offset timestamps are converted to UTC. Existing naive
values cannot recover any historical host DST conversion error.

Repeated subscriptions refresh feed metadata and keep existing download associations
only when the enclosure URL is unchanged and the stored filename still refers
to a contained local file. New episodes start undownloaded. A safely stored feed keeps its directory; distinct feeds with colliding
titles receive numbered directory suffixes. An explicit `--podcast-dir` already
owned by another feed is rejected before either JSON repository changes.
Existing cross-feed directory sharing is also rejected on refresh. Re-add one
feed with a distinct `--podcast-dir` to separate future imports. Old JSON and audio
files stay in their old directory; download associations are not migrated to the
new directory automatically. An unsafe legacy path does not block unrelated
subscriptions, but must be corrected before using that subscription.

Feed requests accept only successful HTTP responses, follow at most five redirects,
and retain at most 8 MiB of uncompressed bytes. Compressed responses are rejected.
Acquisition checks a 30-second elapsed deadline across redirects and chunks, with
HTTP operation timeouts of at most 10 seconds (a blocked operation may run until
its timeout before the elapsed deadline is checked again). Fetch failures raise
`FeedError` and leave subscription and episode JSON unchanged. Relative enclosure
URLs resolve against the final response URL or its Content-Location header;
HTTP Last-Modified remains a date fallback.

## Browse subscriptions

`pdcst podcasts` lists stored subscriptions with their episode counts.
`pdcst episodes amber` lists episode titles for the first subscription whose
title contains `amber`, ignoring case. An empty store, an empty show or an
unmatched show produces no output and succeeds. Both commands read local
metadata under `PDCST_HOME`; they do not refresh feeds or download audio.

## Download

Download an episode which has "rest" in the title from the python podcast:

```shell
$ pdcst download python rest
```

Podcast and episode identifiers match title substrings without regard to case.
A missing podcast or episode returns a concise error and nonzero exit status
before computing download paths, writing metadata, or fetching audio.

# Contribute

## Install Development Version

- Checkout source repository
- Create a virtualenv

Inside the virtualenv install flit:
```shell
$ python -m pip install flit
```

Install a symlinked development version of the package:
```
$ flit install -s
```
## Run Tests

Flit should have already installed pytest:

```shell
$ pytest
```

For an isolated test run, create a writable data directory and set `PDCST_HOME`
to it before running pytest. The package requires Pydantic 1 for its current
storage and settings APIs. Formatting/lint checks are configured in
`.pre-commit-config.yaml` (Black, isort, pyupgrade, Flake8 and file hygiene).
No type-checking is configured. GitHub Actions runs the synthetic suite on Linux
with Python 3.10 and 3.13 and a temporary data root, after installing a wheel with
the `test` extra. The main suite imports the source checkout; a separate smoke
check imports the installed package in isolated Python mode and runs
`pdcst --help` from outside the checkout. No subscriptions, accounts,
or external services are needed.

The equivalent local commands, in a fresh virtualenv for each interpreter, are:

```shell
export PDCST_HOME="$(mktemp -d)"
export PDCST_TEST_WHEEL=1
python -m pip install build
python -m build --wheel
python -m pip install "$(echo dist/*.whl)[test]"
python -m pytest
```

Use a clean `dist/` directory so it contains only the wheel just built. Run the
configured formatting/lint hooks as applicable before submitting changes.
`PDCST_TEST_WHEEL=1` enables the wheel smoke check; leave it unset for ordinary
editable installs, which skip that check.

`tests/test_browsing_cli.py` invokes the registered browsing commands with
CliRunner in fresh processes, setting `PDCST_HOME` before import. Synthetic
on-disk stores verify output, repository selection and unchanged stored bytes;
guards reject feed requests, downloads and repository writes.

## Storage and failure guarantees

`PDCST_HOME` selects the storage root. Feed-derived directory and episode titles
are converted to single safe components for new subscriptions. Existing stored
directory names and contained legacy directories remain stable. `--podcast-dir` may specify a relative
subdirectory within that root; absolute paths, parent traversal, Windows paths,
and symlinks resolving outside the root are rejected before writing. Filename
patterns must produce a single relative filename. Existing subscriptions with
unsafe paths must be corrected before use. Containment assumes other local
processes do not change directory symlinks while a command is running.

Downloads require an HTTP success response and follow at most five redirects.
Audio defaults to a 2 GiB byte limit (`PDCST_AUDIO_MAX_BYTES`) and a six-hour
elapsed limit (`PDCST_AUDIO_MAX_SECONDS`). Both must be positive; the duration
must be finite. Larger episodes can use higher limits. Declared lengths over the
byte limit are rejected before creating output; each raw chunk is checked before
writing, and elapsed time is checked again before publication. HTTP operations
time out after at most 10 seconds (or the configured duration if smaller), so a
blocked operation may finish its timeout before the elapsed limit is checked.
Budget failures raise `ValueError`, preserve previous audio and download metadata,
and remove only this download's temporary file.
Missing or invalid Content-Length uses indeterminate progress. A valid length
must match the received bytes. Responses with a content encoding other than
identity are rejected. Replacements preserve existing file permissions; new files use 0666 filtered by
the process umask. Files are flushed into a sibling temporary file
and atomically replaced after completion; failures retain the previous file and
do not mark an episode downloaded. Filesystem errors propagate to the caller.

Before transferring audio, downloads claim the destination for the selected episode
GUID under a per-podcast `episodes.json.download.lock`. Concurrent downloads for
that podcast wait for this claim; feed refreshes can proceed during transfer.
Publication rechecks ownership under the JSON repository lock. A destination owned
by another GUID or an unowned existing file is refused without replacing its bytes
or changing download associations. Filename comparisons conservatively ignore case
and normalize Unicode on all hosts. Repository filenames and symlink destinations
(including loops) are refused. Unsafe sibling associations do not block unrelated
downloads. A same-GUID
refresh of its associated file is allowed. Use a distinct filename pattern (for
example, one including `{index}`) to resolve episode name collisions.

Downloads commit only the local filename against the current GUID and captured
enclosure URL, preserving newer titles, dates and indices. If the episode disappears
or its enclosure changes during transfer, the command reports a conflict and the
completed audio's `.pdcst-download-*` recovery path. It leaves the destination intact
and does not associate those bytes with the new enclosure. A feed refresh drops an
association when its enclosure URL changes, retaining the old audio on disk. Move
that unowned audio aside before retrying with the same filename. Enclosure identity
uses the complete URL, including query parameters; feeds that rotate signed or
tracking URLs therefore invalidate associations even if their audio is unchanged. Recovery files can
be inspected/moved manually; they are never automatically adopted. Failed transfers
remove their own staging file. Audio replacement and JSON publication are ordered,
not a crash-atomic transaction: a JSON write failure may leave complete audio at the
destination without an association; preserve/move it before retrying. Publication
errors that leave a completed staging file also report its recovery path.

JSON repositories use stable `.json.lock` files and POSIX advisory locks to
serialize cooperating writers across reload and atomic replacement. Do not
remove either kind of lock file while commands are running. Failed serialization
or writes leave the previous JSON intact; leftover temporary files after a killed process
can be removed when no commands are running. Existing corrupt JSON must be
restored from backup; it is not silently overwritten. Mutations invalidate the
same instance's cached list; use a new instance to observe other writers.

Subscription refresh holds the podcast lock while importing episodes under their
own lock, then publishes the subscription record last. Corrupt episode JSON or
an episode read/write failure leaves subscription bytes, directory pointers and
counts unchanged. If subscription publication fails, imported episode metadata
may already be updated; retrying preserves valid download associations. This is
an ordered two-file update, not a crash-atomic transaction. New directories and
stable lock files may remain after a failed import.
