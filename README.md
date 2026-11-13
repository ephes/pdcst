# pdcst

A small command line client for downloading podcast episodes.
It's in pre alpha status (just started a week ago).

# Installation

Python 3.10 or newer is required. The current storage/settings APIs use Pydantic 1.

```shell
$ pipx install pdcst
```

The default data root is `~/pdcst`; set `PDCST_HOME` to use a different path.
No prerequisite `mkdir` is needed. Help and browsing work with a missing root
without creating it. A writing operation such as `add` creates the root as needed;
an existing file at the root is refused with a concise CLI error. If `add` cannot
create or update local storage (for example, a parent is a file or write permission
is denied), it reports a bounded filesystem error. Check `PDCST_HOME` and its
permissions before retrying; underlying exception details are not printed.

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

Repeated GUIDs are rejected before publication, including identical repeated
entries. Stored episode/subscription rows and incoming repository batches must
also have unique identities. A duplicate error reports only row positions, never
identity values; feed positions count usable entries only. The API raises
`FeedError` for duplicate feed entries and `DuplicateIdentity` (a `ValueError`)
for duplicate stored or incoming repository rows. CLI commands report these as
concise errors. Restore or correct ambiguous legacy JSON before retrying. Refusal
preserves existing JSON and audio.

Repeated subscriptions refresh feed metadata and keep existing download associations
only when the enclosure URL is unchanged and the stored filename still refers
to a contained regular file without a symlink. The association must have unique
ownership among stored episodes: case/Unicode-equivalent filenames and aliases
to the same file are refused. A conflicting association is cleared for each
refreshed episode; unrelated records stay unchanged. Symlink loops, missing files
and repository/recovery filenames are also unusable. Refresh never deletes linked
or conflicting audio bytes. New episodes start undownloaded. A safely stored feed keeps its directory; distinct feeds with colliding
titles receive numbered directory suffixes. An explicit `--podcast-dir` already
owned by another feed is rejected before either JSON repository changes.
Directory ownership uses NFC Unicode normalization and case folding on every host.
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
Every browse reloads current JSON without writing it, including repeated API calls
using the same repository instance. Cooperating writers are visible on the next
read. Default API repositories are created per call using the current settings.

## Download

Download an episode which has "rest" in the title from the python podcast:

```shell
$ pdcst download python rest
```

Podcast and episode identifiers match title substrings without regard to case.
Downloads require a unique match for both titles; a unique exact title takes
precedence over partial matches. Empty identifiers and ambiguous matches return
a bounded error with the match count and disambiguation guidance. Use an exact
title or a more specific substring; duplicate exact titles must first be corrected
in the metadata. Browsing retains its first-substring-match behavior.
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

For an isolated test run, set `PDCST_HOME` to a writable temporary path
before running pytest. The package requires Pydantic 1 for its current
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
`tests/test_storage.py` directly covers binary/text publication, permission-copy
and descriptor cleanup with injected stream/OS failures and interruption, using
synthetic files. It also verifies that a failed exclusive temporary-file creation
cannot remove a preexisting file.
`tests/test_cli_storage.py` also exercises fresh-process help/browse/add with a
missing synthetic home, file-root refusal, private store-read diagnostics and
filename errors before mutation. Permission failures are injected rather than
relying on host permission behavior.

## Storage and failure guarantees

`PDCST_HOME` selects the storage root. Feed-derived directory and episode titles
are converted to single safe components for new subscriptions. Existing stored
directory names and contained legacy directories remain stable. `--podcast-dir` may specify a relative
subdirectory within that root; absolute paths, parent traversal, Windows paths,
and symlinks resolving outside the root are rejected before writing. Filename
patterns must produce a single relative filename. Supported placeholders are
`{index}`, `{title}` and `{file_format}`. Integer formatting such as `{index:03}`
and escaped literal braces are supported; attribute/index traversal, conversions
and nested fields are rejected with a bounded `FilenamePatternError` (`ValueError`)
before metadata publication or audio transfer. Both `add` and `download` report
pattern failures as concise CLI errors. Existing subscriptions with
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
The transfer helper `download_with_progress` raises `DownloadError` (a `ValueError`)
for HTTP, transport and budget failures. Import these with
`from pdcst.download import DownloadError, download_with_progress`; the exception
is also available as `pdcst.DownloadError`. The command function
`pdcst.download(podcast, episode)` converts expected failures to `typer.BadParameter`,
including budget errors that previously escaped as `ValueError`.
CLI download failures report a concise nonzero error without private URL userinfo, query
or fragment values, or transport exception chains. HTTP failures include only
their status code. Transfer failures preserve previous audio and download metadata,
and remove only this download's temporary file.
Missing or invalid Content-Length uses indeterminate progress. A valid length
must match the received bytes. Responses with a content encoding other than
identity are rejected. Replacements preserve existing file permissions; new files use 0666 filtered by
the process umask. Files are flushed into a sibling temporary file
and atomically replaced after completion; failures retain the previous file and
do not mark an episode downloaded. Filesystem errors propagate to the caller.
The replacement helper closes its descriptor even if opening the stream fails;
flush, permission-copy, sync, close or rename failures remove only its owned
temporary file and preserve the destination. Unrelated temporary files remain.

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
not a crash-atomic transaction. For an ordinary JSON serialization/write failure,
publication moves completed audio back to its `.pdcst-download-*` staging path
and restores any previous owned audio, keeping the old JSON association valid.
The warning reports the completed staging path; retrying the command is safe.
Recovery files are retained for manual inspection and are not adopted on retry.
If a rollback rename also fails, the initiating exception is preserved and a
bounded warning reports the exact paths of the retained completed and previous
audio. Restoration proceeds only when the destination has been safely vacated;
it never overwrites completed bytes after a failed move. The old JSON remains
unchanged, but its audio may need manual restoration. Before retrying, move any
completed audio still at the destination to a reserved `.pdcst-download-*` path,
and restore previous audio from the reported `.previous` backup if needed.
Retries never adopt or remove reserved recovery files. A process crash can also
require manual recovery.

Once audio and JSON have both been published, failure to remove the previous
backup is a cleanup warning: the download succeeds, and `download_episode`
returns the completed destination path. The warning names the retained
`.pdcst-download-*.previous` file without filesystem exception details. That
backup may be removed manually after checking the new audio and metadata; no
repeat transfer is needed.

JSON repositories use stable `.json.lock` files and POSIX advisory locks to
serialize cooperating writers across reload and atomic replacement. Do not
remove either kind of lock file while commands are running. Failed serialization
or writes leave the previous JSON intact; leftover temporary files after a killed process
can be removed when no commands are running. Metadata reads require a regular
UTF-8 JSON file of at most 16 MiB (including
files that grow during the read). Writes enforce the same encoded byte ceiling
before replacement; oversized batches raise `StoreWriteError` (`ValueError`)
and preserve the previous JSON and audio associations. Subscription refresh
checks its subscription batch before importing episode metadata.
Missing stores remain empty. Nonregular files,
oversize input, malformed JSON/schema and filesystem failures raise
`StoreReadError` (`ValueError`) with the path and a trusted category, never stored
record values. Every CLI command reports these failures concisely with a nonzero
exit status. Restore/correct the named store (or repair its read permissions)
before retrying; writing operations refuse unreadable existing stores and never
silently reset them. Reads reload current JSON;
each subscription listing uses one snapshot for metadata and legacy directories.

Subscription refresh holds the podcast lock while importing episodes under their
own lock, then publishes the subscription record last. Corrupt episode JSON or
an episode read/write failure leaves subscription bytes, directory pointers and
counts unchanged. If subscription publication fails, the previous episode generation (including
download links) is restored while both metadata locks remain held. If restoration
also fails, the original error is preserved and a warning names retained
`.pdcst-refresh-*.previous` and `.refreshed` generations for manual recovery.
Do not remove these files before checking both repositories; restore the previous
episode generation before retrying. This is
an ordered two-file update, not a crash-atomic transaction. New directories and
stable lock files may remain after a failed import.

Feed refreshes hold a stable per-feed POSIX advisory lock in `.feed-locks/`
from before acquisition until publication finishes. Concurrent refreshes of the
same URL wait and then fetch anew, so an older acquisition cannot overwrite a
newer one. Different feeds acquire independently and share the subscription
lock only during publication. Lock order is feed, subscription, then episode;
refresh never takes the download claim lock. Failed refreshes release the lock
and can be retried. Do not remove `.feed-locks/` files while commands are running.
