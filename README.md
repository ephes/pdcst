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
episodes remain available. On first import they receive chronological indices
(with GUID order breaking equal-date ties). Refresh retains every stored GUID and
its index, including entries absent from a rolling feed. New GUIDs receive indices
after the retained maximum in date/GUID order, even for newly discovered older
episodes. Listing follows stored index order; subscription counts describe the
retained inventory, not just the current feed window. Download associations keep
their existing filenames. Stores written by earlier releases may hold duplicate
indices from renumbered feed windows; refresh repairs them by keeping each index
on one episode (a downloaded one first, then the oldest) and moving the others
after the retained maximum.
The selected enclosure's supported type is retained as optional `audio_media_type`
in episode JSON. Filename generation keeps recognized URL path suffixes stable
(case insensitive, excluding query strings). Otherwise it uses this MIME mapping:
MPEG/MP3 → `mp3`, MP4/x-m4a → `m4a`, AAC → `aac`, Ogg → `ogg`, Opus → `opus`,
WAV/x-wav → `wav`, FLAC/x-flac → `flac` (all types have the `audio/` prefix).
Thus an `audio/mpeg` enclosure at `/download` becomes an `.mp3` file. Old records
without the field remain readable and retain their former filename fallback;
refreshing the feed populates the field. Existing `audio_file` associations stay
valid when only the type changes; existing bytes are not renamed on refresh.

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

Stored feed URLs, episode GUIDs and enclosure URLs must be nonempty JSON
strings. Their exact spelling is preserved. A malformed identity reports an
invalid schema (`StoreReadError`) and leaves both metadata stores unchanged
during refresh.

Repeated subscriptions refresh feed metadata and check download associations
for every retained episode, including those outside the current feed window.
Refresh looks up each filename relative to the held episode directory without
following symlinks. It clears associations to missing or nonregular files,
repository/recovery names, and files with ambiguous ownership, including
case/Unicode-equivalent names and aliases to the same file. Episodes in the
current feed also lose their association if the enclosure URL changed.
Historical episodes keep their feed metadata and usable audio references.
Refresh also clears references to empty files and files over
`PDCST_AUDIO_MAX_BYTES`, with a warning reporting the number of refused
references. Files exactly at the limit remain eligible.
A permission or I/O error while checking a retained file aborts the refresh
before either metadata store changes; a stored name that cannot exist (too long
or an invalid byte sequence for the filesystem) counts as missing. Refresh leaves all physical audio entries
untouched. New episodes start undownloaded.
A safely stored feed keeps its directory. Distinct feeds with colliding titles
receive numbered directory suffixes. An explicit `--podcast-dir` already
owned by another feed is rejected before either JSON repository changes.
Directory ownership uses NFC Unicode normalization and case folding on every host.
Refresh uses one validated subscription catalog under the writer lock for
directory selection, ownership checks and the metadata update. Subscription
preflight builds one ownership index, resolving
each other subscription directory once; numbered suffix trials use that index.
Existing cross-feed directory sharing is also rejected on refresh. Re-add one
feed with a distinct `--podcast-dir` to separate future imports. Old JSON and audio
files stay in their old directory; download associations are not migrated to the
new directory automatically. An unsafe legacy path does not block unrelated
subscriptions, but must be corrected before using that subscription.

Feed requests accept only successful HTTP responses, follow at most five redirects,
and retain at most 8 MiB of uncompressed bytes. Compressed responses are rejected. Decimal Content-Length headers are compared
against the byte ceiling without converting arbitrarily long integers; leading
zeros are accepted.
Acquisition checks a 30-second elapsed deadline across redirects and chunks, with
HTTP operation timeouts of at most 10 seconds (a blocked operation may run until
its timeout before the elapsed deadline is checked again). Fetch failures raise
`FeedError` and leave subscription and episode JSON unchanged. Transport failures
report only a bounded category (including HTTP status or timeout when available).
Library exceptions retain no underlying transport cause/context, so their messages
and formatted tracebacks omit private URL credentials, queries and fragments.
Failed HTTP response bodies are not read for diagnostics.

Before feedparser materializes entries, a streaming XML preflight admits at most
64 levels of nesting, 100,000 elements, 10,000 RSS items/Atom entries, 50,000
link/enclosure elements and 200,000 attributes/namespace declarations in total.
Charset decoding and entity repair each have a 16 MiB UTF-8 ceiling. DOCTYPE declarations (including
external DTDs) are refused before expansion or external access. The acquisition
deadline is also checked during preflight, after parsing and during enclosure
traversal; parser calls are synchronous, bounded by these input/work limits.
These fixed limits refuse the refresh before either metadata store changes.

Malformed or incomplete XML is refused before using partial parser entries,
including truncated items, mismatched tags and malformed trailing XML. Parser
failures also refuse unknown encodings and undeclared namespaces. Complete feeds
with feedparser's `NonXMLContentType` or `CharacterEncodingOverride` warnings
remain accepted: a wrong MIME type or a successfully repaired encoding declaration
alone does not invalidate an otherwise complete feed. Recoverable entity errors
(a bare `&` or an HTML entity such as `&nbsp;` without a DOCTYPE) are repaired
by the lenient parser and accepted only when a strict re-parse with those
constructs escaped succeeds; any truncation, mismatched tag, undeclared
namespace or trailing junk elsewhere in the document still refuses the feed.
Refusal preserves the previous subscription, episode inventory and audio
associations.
Relative enclosure
URLs resolve against the final response URL or its Content-Location header;
HTTP Last-Modified remains a date fallback.

## Browse subscriptions

`pdcst podcasts` lists stored subscriptions with their episode counts.
`pdcst episodes amber` uses the same case-insensitive podcast selection as
`download`: it prefers a unique exact title and otherwise requires a unique
substring. Blank or ambiguous identifiers exit with status 2. An unmatched
title, including when the store is empty, also exits with status 2 and reports
`No matching podcast`. A matching subscription with no episodes succeeds with
no output. Both commands read local
metadata under `PDCST_HOME`; they do not refresh feeds or download audio.
If the selected episode directory cannot be resolved safely, `episodes` exits
with status 2 and a directory diagnostic. Check the subscription directory and
its symlink targets under `PDCST_HOME`.
Every browse reloads current JSON without writing it, including repeated API calls
using the same repository instance. Cooperating writers are visible on the next
read. Default API repositories are created per call using the current settings.
For a legacy subscription without a saved directory, browsing prefers the
contained legacy directory if it exists and otherwise uses the sanitized name.
A directory shared by multiple feeds is refused for both explicitly saved and
legacy directories. Ownership uses resolved paths, NFC normalization and case
folding. The `podcasts` command fails and names the affected subscriptions by
escaped title and HTTP(S) origin (scheme, host,
and optional port). Feed credentials, paths, queries, and fragments are omitted.
If an origin cannot be parsed, the CLI displays `feed origin unavailable`.
The `episodes` and `download` commands refuse
those feeds while other subscriptions stay usable. The API raises
`SharedDirectoryError` (a `StoreReadError` whose message contains no stored
values; its `shared` and `podcasts` attributes hold the affected and all
subscriptions). Re-add one feed with a distinct `--podcast-dir` to resolve the
conflict. The refusal leaves metadata and audio untouched.

Titles come from feeds and are displayed safely: `add`, `podcasts` and `episodes`
print control characters (such as CR, LF, TAB, ESC and C1 controls), Unicode
line/paragraph separators and bidirectional override/isolate marks as visible
`\uXXXX` escapes, so a feed cannot rewrite, hide or reorder terminal output or add
lines to a listing. `podcasts` uses a 160-character display budget for the title
and episode count together. It marks shortened titles with `…` and keeps
each escape intact. Stored titles keep their original text, and title matching for
`episodes` and `download` uses that original text. Library callers that print
`Podcast`/`Episode` objects themselves can use `pdcst.terminal_text()`.

## Download

Download an episode which has "rest" in the title from the python podcast:

```shell
$ pdcst download python rest
```

Podcast and episode identifiers match title substrings without regard to case.
Audio fetches require HTTP 200 without Content-Range. Unsolicited partial
responses are refused before staging; previous audio and metadata survive and a
full-response retry is supported.

The final response's `Content-Type` is checked before staging. Supported types
are the `audio/` types listed under Subscribe above, plus `application/ogg` and
`application/mp4` containers. Missing/blank types, `application/octet-stream` and
`binary/octet-stream` remain accepted for CDN compatibility. Type matching ignores
case and parameters. All other declarations, including HTML, XML and JSON error
documents (also `+xml`/`+json` variants), are refused without replacing saved audio
or metadata. A mislabeled CDN must use one of the supported types; a filename
extension does not override its response declaration. This checks the declared
type, without decoding or sniffing the media bytes.

Downloads require a unique match for both titles; a unique exact title takes
precedence over partial matches. Empty identifiers and ambiguous matches return
a bounded error with the match count and disambiguation guidance. Use an exact
title or a more specific substring; duplicate exact titles must first be corrected
in the metadata. Download selection counts matches and checks for exact titles
in one pass. Browsing stops matching at the first substring match.
A missing podcast or episode returns a concise error and nonzero exit status
before computing download paths, writing metadata, or fetching audio.
The first successful download for a legacy subscription saves the selected
directory in `podcasts.json`. Migration holds the per-feed lock through
transfer and pinning. The catalog writer lock is held briefly to check ownership
before transfer and again to reload the latest catalog, recheck ownership and
save the pin. Other feeds can publish during the transfer. If another command
has already pinned the subscription, the download uses its saved directory.
A directory selected for migration that is reserved for repository storage or
already used by another feed is refused before transfer. If the subscription
changes or another feed claims the directory during transfer, the pin is refused
and the command reports where the audio was saved. Resolve the conflict before
retrying. The catalog also checks the admitted episode directory immediately
before saving the pin. If that directory has moved or been replaced since the
audio was saved, the command reports an incomplete pin. The completed audio
and its episode metadata stay in the original directory. Restore the original
layout and verify the saved files before retrying. Later commands use a saved
directory even if an alternate legacy directory appears.

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
`.pre-commit-config.yaml` (Black, isort, pyupgrade, Flake8 and file hygiene);
run them all with `pre-commit run --all-files` (for example via
`uvx pre-commit run --all-files`). No type-checking is configured. GitHub
Actions runs the synthetic suite on Linux with Python 3.10 and 3.13 and a
temporary data root, after installing a wheel with the `test` extra. The main suite imports the source checkout; a separate smoke
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
and symlinks resolving outside the root are rejected before writing. Directory components may not use `podcasts.json`, `episodes.json`, their metadata
or download lock names, `.feed-locks`, or any `.pdcst-refresh-`/`.pdcst-download-`
name (including intent and recovery names). The check ignores case, normalizes
Unicode and includes resolved in-root aliases; it applies at every nesting level.
Explicit reserved directories are refused before creating locks/directories or
fetching the feed. Ordinary custom/hidden directories such as `.shows` remain
supported. Legacy stored reserved directories must be corrected before refresh.
Filename patterns must produce a single relative filename. Supported placeholders are
`{index}`, `{title}` and `{file_format}`. Integer formatting such as `{index:03}`
and escaped literal braces are supported; attribute/index traversal, conversions
and nested fields are rejected with a bounded `FilenamePatternError` (`ValueError`)
before metadata publication or audio transfer. Subscription preflight checks the
entire retained inventory, including episodes absent from the current feed, after
assigning stable indices. Generated filenames must be distinct under NFC Unicode
normalization and case folding on every host. A collision refuses both metadata
updates; use a pattern including `{index}`. Existing downloaded filenames remain
associated without renaming. Both `add` and `download` report
pattern failures as concise CLI errors. Formatting accepts the standard spec
syntax with bounded numbers (every width/precision 0–255): an optional fill and
alignment (`<`, `>`, `^`, `=`); for `index` also sign, `#`, zero padding,
grouping (`,`/`_`) and the integer types `b`, `d`, `o`, `x`, `X`, `n` (not `c`);
string fields accept width (not starting with `0`) and/or `.precision`,
optionally followed by `s`. For example, `{index:03d}_{title:.100}.{file_format}`
limits the title to 100 characters and `{index:>3}` right-aligns the index.
Only referenced fields are prepared, so `{index:03}.mp3` needs neither title
sanitization nor extension lookup. Repeated fields reuse their prepared values.
Other format syntax is refused before formatting. Patterns are limited to 4,096
characters and the complete basename to 255 UTF-8 bytes, including literals and
repeated fields; excess output is rejected rather than truncated. Existing
subscriptions with unsafe paths must be corrected before use. Downloads open each
component of the admitted episode directory without following symlinks and keep
the directory descriptors until completion. Claims, transfer stages, metadata,
intents, publication and rollback use that admitted directory. A replaced root,
ancestor or episode directory refuses publication; previous JSON and audio stay
paired in the admitted (possibly renamed) directory. The replacement tree is
untouched. Stop other local writers, restore the original directory layout and
inspect retained stages before retrying; recovery paths refer to the original
layout. Ancestors that grant only search (`x`) permission are opened search-only
where the platform supports it (`O_PATH`/`O_SEARCH`); the episode directory itself
must be readable. A symlinked component (including a library caller passing an
unresolved path) is refused; the CLI passes resolved paths. Access or creation
failures are reported with their system reason instead of as identity changes.
Subscription metadata reads and writes also hold the admitted root directory.
The CLI resolves the configured storage root (`PDCST_HOME`) first, so a
symlinked ancestor such as macOS `/tmp` keeps working; errors name the storage
root, episode or feed lock directory that failed. A relative storage root passed
by a library caller is made absolute when it is admitted.
Refresh opens the episode directory relative to the held root and holds it as
well (an episode path that no longer lies under the admitted root, for example
after `PDCST_HOME` is retargeted during the fetch, is refused): locks, backups,
intents, replacements,
cleanup and rollback use those descriptors. A changed root/ancestor refuses
publication without writing into its replacement; rollback stays in the admitted
(possibly renamed) tree. Restore the original directory layout before retrying.
Browsing a missing root still creates no directories.

Before transferring into an unowned audio filename, download scans the admitted
folder for Unicode/case-equivalent names. Admission and publication share a fixed
budget of 10,000 directory entries and one second of active scan time, including
iterator acquisition/advancement and name normalization. Admission may process
up to 4,999 entries and use up to half the scan time, so the mandatory
publication recheck of the same folder plus the transfer stage still fits the
remainder; folders with 5,000 or more entries are refused before any transfer.
At either entry cap, one additional iterator step checks for the end of the
listing within the deadline. An extra entry causes refusal before its name is
normalized. Transfer time and lock waiting do not consume scan time. An incomplete scan refuses the download explicitly; unseen entries
are never assumed free. If the budget expires only on the publication recheck
(for example because the folder grew during the transfer), completed audio is retained as a recovery stage and
metadata stays unchanged. Move unrelated directory entries aside or select a
smaller dedicated podcast directory before retrying. Known owned destinations
still receive identity/regular-file checks without scanning unrelated entries.
Directory enumeration is synchronous: a blocked filesystem operation may return
after the deadline before refusal is reported.

Library transfer callbacks for `EpisodeRepository.download_episode` now receive
`transfer(url, stage_path, *, directory_fd=...)`. The path is for naming and
diagnostics; callbacks must use `stage_path.name` with the supplied `dir_fd` for
all file access, or use `replacement_file(stage_path, binary=True,
directory_fd=directory_fd)`. Do not close that borrowed descriptor or reopen the
stage through its full pathname. The built-in `download_with_progress` accepts
this keyword and follows the contract. Standalone calls without it retain their
pathname interface.

Downloads require an HTTP success response and follow at most five redirects.
Redirect responses are closed without reading their bodies. Relative locations,
cookies and authorization headers follow HTTPX's redirect rules. If HTTPX
prepares the next request within the redirect limit, a response-close failure
logs a warning without URL details and the download continues. A redirect
without a prepared request fails with `DownloadError`. All hops and
the final body share one elapsed deadline; only the final body is retained and
counted against the audio byte limit.
Audio progress updates are spaced at least a tenth of a second apart during
streaming. At the end of the stream the display receives the exact byte count.
Audio defaults to a 2 GiB byte limit (`PDCST_AUDIO_MAX_BYTES`) and a six-hour
elapsed limit (`PDCST_AUDIO_MAX_SECONDS`). Both must be positive; the duration
must be finite. Larger episodes can use higher limits. Declared lengths over the
byte limit are rejected before creating output. The elapsed deadline is checked
after staging opens and again after progress setup, before reading the body.
Each raw chunk is checked before writing, and elapsed time is checked again
before publication. HTTP operations
time out after at most 10 seconds (or the remaining elapsed budget if smaller), so a
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
Response and HTTP client close failures leave an earlier transfer or replacement
error intact. After successful audio publication, either close failure produces
a warning without response or URL details, and the transfer returns the output
path.
Cleanup warnings are best effort. A logging handler failure leaves an accepted
redirect in progress and allows completed audio to be associated with its episode.
Missing or malformed Content-Length (including non-ASCII digits) uses
indeterminate progress. ASCII decimal lengths are compared to the byte budget
before integer conversion, ignoring leading zeros; arbitrarily large declared
values raise `DownloadError` before creating output. A valid length
must match the received bytes. A response must contain at least one body byte;
empty success responses raise `DownloadError` and preserve previous audio and
metadata, including zero-length and empty chunked responses. A directory-sync
failure while staging a repository transfer is reported as `DownloadError`.
The repository removes its stage and preserves previous audio and metadata.
For direct `download_with_progress` calls, a directory-sync failure after
replacement raises `pdcst.storage.PublicationDurabilityError`. Its `path`
contains the complete replacement bytes, with uncertain durability. The
response checks verify transfer completeness without validating the audio codec. Responses with a content
encoding other than identity are rejected. Replacements preserve the permissions
of existing regular files and refuse symlinks and other nonregular destinations
before publication. New files use 0666 filtered by the process umask.
Files are flushed into a sibling temporary file
and atomically replaced after completion. While empty, the sibling's
umask-filtered creation mode is captured; before its first payload write it is
restricted to 0600. After writing and flushing, the destination's existing mode
(or the captured new-file mode) is restored for publication. The helper never
changes the process umask. The containing directory is then synced
before success. Failures before replacement retain the previous file and do not
mark an episode downloaded. Filesystem errors propagate to the caller.
The replacement helper closes its descriptor even if opening the stream fails;
flush, permission-copy, file-sync, close or rename failures preserve the
destination. Cleanup attempts to remove only the owned temporary file, and an
unlink error leaves the original failure in place. A successful replacement
consumes the temporary name. A subsequent directory-sync failure or
interruption raises `pdcst.storage.PublicationDurabilityError` (`OSError`).
The new file is already visible, but its durability across power loss is uncertain.
The CLI reports that state without raw filesystem exception details. Unrelated
temporary files remain.

Before transferring audio, downloads claim the destination for the selected episode
GUID under a per-podcast `episodes.json.download.lock`. Concurrent downloads for
that podcast wait for this claim; feed refreshes can proceed during transfer.
Publication rechecks ownership under the JSON repository lock. A destination owned
by another GUID or an unowned existing file is refused without replacing its bytes
or changing download associations. Ownership checks compare filenames using NFC
normalization and case folding on all hosts before looking up other episodes' files.
Matching associations use one no-follow stat per lookup. A matching association
can reserve a filename even when its file is missing. An existing file must be
regular for its association to count. Repository filenames and symlink destinations
(including loops) are refused. Unsafe sibling associations do not block unrelated
downloads. A same-GUID
refresh of its associated file is allowed only when the existing destination is a
regular file without a symlink. Restored metadata cannot authorize moving a
directory, FIFO, device or other special entry aside. This is checked before
transfer and again during publication; an invalid destination leaves its entry
and metadata untouched, without creating a publication intent. A destination
generation that changes after the intent is written is also refused before old
audio is moved, and the unused intent is retired.
Download validates the stored filename pattern syntax before reading episodes.
The rendered filename is checked against the byte limit after episode selection.
Use a distinct filename pattern (for
example, one including `{index}`) to resolve episode name collisions.

The repository also validates the transfer callback's result independently of the
HTTP helper. Returning without writing, leaving an empty or missing stage, or
substituting a symlink/special file is a download conflict. A completed stage must
be a regular file with 1 through `PDCST_AUDIO_MAX_BYTES` bytes. A nonblocking,
no-follow descriptor remains open through publication; its device/inode, size and
nanosecond modification/change timestamps must match the named file. These
checks run before recording intent and again before moving audio. Mode copying
uses that descriptor, so it cannot chmod a substituted symlink's target. A changed
stage is refused while preserving the previous audio and metadata. Ordinary
invalid stages are removed; a directory substituted at the reserved stage path
is left intact for manual inspection and is never traversed or removed. These
checks detect changes at admission/publication boundaries; they assume local
processes do not replace individual files after the final generation check.
Directory operations remain bound to the admitted descriptors. These checks do
not validate audio codecs.

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

Audio directory entries are synced before JSON publication; JSON replacement
also syncs its parent before the previous audio backup may be retired. If JSON
is visible but its directory sync fails, matching new audio and metadata stay
together, the previous audio backup is retained, and the command reports uncertain
durability. Inspect these files before retrying; this is not a rollback or a
crash-atomic transaction. A failure before JSON replacement restores prior audio.

Once audio and JSON, or both refreshed metadata stores, have been published and
synced, an intent-retirement unlink or directory-sync error is a **committed
success with recovery cleanup pending**. The API returns the completed download
path or refreshed podcast, and the CLI succeeds with a bounded warning naming
the intent paths. Matching new data and remaining backups are kept; cleanup alone
never triggers compensation. A marker may already be absent after a sync failure.
Do not repeat the transfer or refresh. Stop all commands, inspect the named paths
and retained generations, and follow Interrupted-publication recovery below to
verify the committed generation and retire remaining markers/backups. An unresolved
remaining marker continues to fence later reads. Backup-only refresh cleanup
failures also warn without converting the committed refresh into a failure.

Once audio and JSON have both been published and synced, failure to remove or
sync the retirement of the previous backup is a cleanup warning: the download
succeeds, and `download_episode` returns the completed destination path. The warning names the
`.pdcst-download-*.previous` path (it may already be absent if only retirement
sync failed) without filesystem exception details. That
backup may be removed manually after checking the new audio and metadata; no
repeat transfer is needed.

JSON repositories use stable `.json.lock` files and POSIX advisory locks to
serialize cooperating writers across reload and atomic replacement. Metadata,
download and feed lock descriptors must be regular files, opened
without following symlinks and without blocking on special files. Their named
inode is checked again after acquisition. Invalid locks raise a bounded
`StoreWriteError`; move an invalid lock aside only when no commands are running.
(A read that only waits for a publication marker's lock reports both cases as a
`StoreReadError` naming the marker, with the `StoreWriteError` as its cause; see
"Interrupted-publication recovery".)
Lock contention waits at most 30 seconds per acquisition by default. Set
`PDCST_LOCK_WAIT_SECONDS` to a positive finite duration in seconds to change
that limit (including for feed and download claim locks). The wait uses a
monotonic clock; expiry raises `StoreWriteError` with a busy/retry diagnostic
and closes the descriptor. Retry the command after the other writer finishes,
or increase the limit for intentionally long operations. Already held locks
remain held for their operation; the limit bounds waiting, not operation time.
Do not remove any lock file while commands are running. Failed serialization
or writes before replacement leave the previous JSON intact; leftover temporary files after a killed process
can be removed when no commands are running. If the staging descriptor fails
to close, publication stops before replacement. An earlier staging error
remains the reported failure if descriptor close also fails.
Metadata reads require a regular
UTF-8 JSON file of at most 16 MiB (including
files that grow during the read). Writes enforce the same encoded byte ceiling
before replacement; oversized batches raise `StoreWriteError` (`ValueError`)
and preserve the previous JSON and audio associations. Subscription refresh
checks its subscription batch before importing episode metadata.
Missing stores remain empty. Metadata symlinks (including dangling links) are
refused without reading or modifying their targets. Move the link aside and
restore a regular JSON file at the named path before retrying. Nonregular files,
oversize input, malformed JSON/schema and filesystem failures raise
`StoreReadError` (`ValueError`) with the path and a trusted category, never stored
record values. Repeated JSON object members are invalid JSON, including identical
values, escaped spellings of the same name, nested feed fields and unknown fields;
they are refused before model construction. Original bytes remain untouched for
manual correction or recovery from a trusted backup. Every CLI command reports
these failures concisely with a nonzero exit status. Restore/correct the named
store (or repair its read permissions)
before retrying; writing operations refuse unreadable existing stores and never
silently reset them. Reads reload current JSON;
each subscription listing uses one snapshot for metadata and legacy directories.

Subscription refresh holds the podcast lock while importing episodes under their
own lock, then publishes the subscription record last. Corrupt episode JSON or
an episode read/write failure leaves subscription bytes, directory pointers and
counts unchanged. If subscription publication fails, the previous episode
generation (including download links) is restored while both metadata locks remain held.
The private rollback copy keeps the original `episodes.json` permissions. If the
refreshed `episodes.json` is replaced but its directory sync fails, the refresh is
rolled back the same way and reported as a failed write (`StoreWriteError`), not
as a publication with uncertain durability. If restoration
also fails, the original error is preserved and a warning names retained
`.pdcst-refresh-*.previous` and `.refreshed` generations for manual recovery.
Do not remove these files before checking both repositories; restore the previous
episode generation before retrying. Names beginning `.pdcst-refresh-` are reserved
for recovery metadata and cannot be downloaded or retained as audio associations.
A subscription replacement whose final
directory sync fails is already visible: its matching refreshed episodes stay
in place and the error reports uncertain durability. This is
an ordered two-file update, not a crash-atomic transaction. New directories and
stable lock files may remain after a failed import.

Feed refreshes hold a stable per-feed POSIX advisory lock in `.feed-locks/`
from before acquisition until publication finishes. The directory is opened
relative to the held storage root without following symlinks, and the hashed lock
is opened through its directory descriptor. Symlinked lock directories, including
in-root aliases, are refused before fetching; replacement while waiting or fetching
refuses publication. Restore the admitted directory layout before retrying.
Concurrent refreshes of the
same URL wait and then fetch anew, so an older acquisition cannot overwrite a
newer one. Different feeds acquire independently and share the subscription
lock only during publication. Lock order is feed, subscription, then episode;
refresh never takes the download claim lock. Failed refreshes release the lock
and can be retried. Do not remove `.feed-locks/` files while commands are running.

Metadata reads compare the opened descriptor's device/inode, size and nanosecond
modification/change timestamps after the bounded read. The byte count must
match the admitted size before JSON parsing. In-place rewrites are
refused as a changed generation, including same-size rewrites. Atomic replacement
can still return the coherent old descriptor (unlink-only change timestamps do
not invalidate unchanged bytes). Retry after an external writer finishes.
Stored episode indices must be JSON integers greater than zero; subscription
counts must be JSON integers at least zero. Booleans, strings and fractional
numbers are refused through the private schema error, without rewriting the store.

### Interrupted-publication recovery

Before publishing a refresh, pdcst syncs backups of both existing JSON stores and
writes `.pdcst-refresh-intent.json` in the episode directory and the storage root.
The intent names `episodes`, `previous_episodes`, `refreshed_episodes`, `podcasts`,
`previous_podcasts` and both `markers`. A null previous path means that store did
not exist. The refreshed generation is initially at `episodes`; a failed rollback
may also retain it at `refreshed_episodes`. Markers retire only after both new
stores are synced or compensation has restored the old generation and synced it.

Before an audio replacement, `.pdcst-download-intent.json` records `metadata`,
`target`, `stage` and `backup` in the episode directory. A null backup means no
previous audio existed. It is synced before moving any old audio. Both audio and
JSON are synced before retiring this intent. A completed transfer interrupted
before intent creation is an unassociated reserved stage and cannot displace audio.

An unresolved intent blocks reads and mutations of the affected repository,
including episode listing and download; a refresh root marker also blocks
subscription browsing and all subscriptions. The error names the marker. Writers publish
and retire their markers while holding the owning store lock (the episode store
lock for episode-directory markers, the subscription store lock for the root
marker). Readers check markers before and after reading bytes. A marker appearing during
a read discards those bytes; after waiting for its owner lock, the reader rereads
a settled generation under that lock. The same check follows an incomplete or
changed-generation read. When a marker is observed, the reader retries once
under its owner lock. This does not provide a multi-store atomic
read. A command that sees a marker waits for that lock, within the lock wait
limit, and checks again: an in-progress publication only delays it, while a
marker still present under the lock is unresolved. If the wait limit expires or
the lock cannot be opened (for example a read-only store) while the marker is
still present, the command fails with a `StoreReadError` that names the marker
and the lock problem; retry once the writer finished. No
automatic winner is inferred, even if the pair appears complete. Help remains
available. Do not remove locks or retry with a different filename to bypass this
conflict. A visible publication with uncertain durability retains its marker and
backups too; an ordinary successful rollback clears the marker.

Recovery is manual, with **all pdcst commands stopped**:

1. Read the named intent as JSON and copy every existing named generation and
   marker somewhere safe. Missing paths can be expected after a rename; inspect
   the other named locations. Never discard a generation merely because its name
   says `previous`, `stage` or `refreshed`.
2. For refresh, choose a coherent pair. To restore the old pair, copy
   `previous_episodes` to `episodes` and `previous_podcasts` to `podcasts`; when a
   previous path is null, restore that store's absence. Alternatively retain the
   refreshed episodes and correct/verify the matching subscription directory,
   metadata and episode count in `podcasts`. Keep all alternative copies until
   that pair is verified. Do not restore only old episodes against a new count.
3. For audio, move any completed bytes at `target` to a spare reserved
   `.pdcst-download-*` path before restoring old audio from `backup` to `target`.
   Keep `stage` intact. If `backup` is null, restore the previous target absence.
   Verify the `audio_file` association in `metadata`. Alternatively retain the new
   audio only after verifying its enclosure and matching metadata yourself.
4. Flush restored files and sync their containing directories. Only then remove
   every listed refresh marker (or the audio marker) and sync those directories.
   Retry browsing before any new refresh/download. Retained recovery generations
   can be retired manually after verifying the chosen pair.

This detects process interruption; it does not make multi-file publication atomic
or choose a generation after power loss. Once an intent has been durably retired,
a leftover backup only needs manual cleanup; it does not block normal startup.

`tests/test_interrupted_publication.py` kills real CLI subprocesses at publication
barriers, using temporary stores and fake feed/transfer functions. The paired
process tests in `tests/test_download_process_ownership.py` kill a transfer owner,
observe a waiting successor acquire the released kernel claim, update metadata
under its normal JSON lock, and prove that the successor refuses a stale enclosure
or a destination newly owned by another GUID before transfer. A fresh selection
then succeeds; unassociated partial stages remain reserved for manual cleanup.
All child processes are terminated and reaped, including on test failure.
