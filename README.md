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
only when the stored filename still refers to a contained local file. New episodes
start undownloaded. A safely stored feed keeps its directory; distinct feeds with colliding
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
No type-checking or CI workflow is configured.

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
Missing or invalid Content-Length uses indeterminate progress. A valid length
must match the received bytes. Responses with a content encoding other than
identity are rejected. Replacements preserve existing file permissions; new files use 0666 filtered by
the process umask. Files are flushed into a sibling temporary file
and atomically replaced after completion; failures retain the previous file and
do not mark an episode downloaded. Filesystem errors propagate to the caller.

JSON repositories use stable `.json.lock` files and POSIX advisory locks to
serialize cooperating writers across reload and atomic replacement. Do not
remove lock files while commands are running. Failed serialization or writes
leave the previous JSON intact; leftover temporary files after a killed process
can be removed when no commands are running. Existing corrupt JSON must be
restored from backup; it is not silently overwritten. Mutations invalidate the
same instance's cached list; use a new instance to observe other writers.
