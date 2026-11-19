# Unreleased

* Refuse nonregular replacement destinations, including symlinks, and preserve
  existing regular-file permissions.

* Save the resolved directory of a legacy subscription on its first successful
  download so future browsing and downloads use the same episode store.

# 2026-11-17 v0.13

Compatibility notes: `v0.12` was tagged but never published to PyPI, so this
release follows `0.11` there. Incompatible changes: Python 3.10 or newer is
required (3.9 is no longer supported); transfer callbacks must use
the new borrowed `directory_fd` keyword for stage access; `pdcst.download`
raises `typer.BadParameter` for download failures that were `ValueError`;
filename patterns with numbers above 255, the `c` integer type or string widths
starting with `0` are refused (edit them in `podcasts.json` or re-add the
podcast); Pydantic is bounded to 1.x; feeds without usable audio episodes and
malformed feed XML are refused.

* Escape control characters (CR/LF/TAB, C1 controls such as RI, DCS and OSC),
  line/paragraph separators and bidi overrides in feed titles printed by `add`,
  `podcasts` and `episodes` as visible `\uXXXX` text, so a feed cannot rewrite or
  hide terminal output. Stored titles and title matching are unchanged; library
  callers can use the new `pdcst.terminal_text()`.

* Admit final audio response Content-Type before staging. Refuse HTML/XML/JSON
  error documents and other unsupported declarations, preserving audio/metadata.
  Retain missing/blank and generic binary types for CDN compatibility; accept the
  supported audio types and MP4/Ogg containers, ignoring case and parameters.

* Follow audio redirects as streamed requests, closing discarded bodies unread.
  Keep the five-hop ceiling and one elapsed deadline across the chain, and bound
  each request timeout by the remaining time. Preserve HTTPX URL/header rules.

* Admit `.feed-locks` relative to the held subscription root without following
  symlinks, acquire hashed locks through that descriptor, and recheck directory
  identities after waiting and before publication. Refuse aliases/replacements
  without redirecting locks or fetching through invalid lock authority.

* Bind subscription metadata reads/writes and paired refresh backups, intents,
  publication, cleanup and rollback to held no-follow directories. Refuse changed
  root/ancestor identities before publication without touching replacement trees;
  browsing a missing root still creates nothing. The CLI resolves `PDCST_HOME`
  before admission so symlinked ancestors keep working, and admission errors name
  the storage root, episode or feed lock directory. Identity changes while a feed
  lock is held are no longer reported as lock acquisition failures. Refresh opens
  the episode directory relative to the held root, so a root replaced during
  admission receives no directories, locks, backups or intents; an episode path
  outside the admitted root (such as a retargeted `PDCST_HOME`) is refused;
  relative library roots are compared as absolute paths.

* Refuse unsolicited partial audio responses (including Content-Range on HTTP
  200) before staging. Preserve existing audio/metadata for a full-response retry.
* Compare feed Content-Length against its byte ceiling as normalized decimal
  text, refusing huge values with a bounded FeedError and accepting leading zeros.

* Bound unowned audio destination scans to a shared 10,000-entry/one-second
  active scan budget across admission and publication. Admission may use at
  most half, so folders that cannot also pass the publication recheck are
  refused before transfer. Refuse incomplete ownership checks explicitly,
  preserving metadata/audio and releasing locks.

* Anchor downloads (claims, stages, metadata, intents, publication and rollback)
  to no-follow directory descriptors. Refuse replaced directory components
  before commit, preserving old JSON/audio without touching replacement trees.
  Ancestors with only search permission are admitted search-only where
  supported; symlinked components and access/creation failures are reported
  separately from identity changes. Transfer callbacks now receive a borrowed
  `directory_fd` keyword and must use it for stage access; the built-in HTTP
  transfer supports this contract.

* Reserve subscription directory components for metadata, intents, recovery files
  and feed/download locks, including case variants and resolved aliases; reject
  explicit control paths before creating locks or fetching the feed.

* Preflight decoded/repaired XML before feedparser entry materialization, bounding
  depth, elements, entries, links and attributes/namespace declarations; reject
  DOCTYPE declarations before entity expansion and extend elapsed checks through parsing/traversal.

* Report committed audio/paired-metadata publications with intent-retirement
  failures as successful with recovery cleanup pending; preserve matching data
  and remaining backups, name recovery actions, and avoid repeat transfers.

* Fence metadata reads with a post-read publication-intent check; wait/recheck
  and reread under the owner lock rather than accepting overlapping bytes.

* Preflight generated filenames across incoming and retained episodes, refusing
  Unicode/case-equivalent collisions before publishing either metadata store.

* Keep GUID indices stable across rolling/reordered feeds, allocate new indices
  after the retained maximum, and count the full retained episode inventory.
  Repair duplicate indices left by earlier releases on refresh.

* Require regular, non-symlink owned audio destinations before transfer and
  publication; refuse changed generations without moving special entries.
* Independently admit completed transfer stages through a bounded, nonempty
  regular descriptor and matching named generation before audio publication.
  Recheck at publication, copy permissions through the descriptor, and preserve
  previous audio/metadata on callback contract violations. Never traverse or
  remove a directory substituted for a stage.

* Reject metadata changed in place during reads while preserving coherent atomic
  replacement reads.
* Require strict positive stored episode indices and nonnegative integer counts.
* Persist refresh/audio publication intents before replacing generations; refuse
  unresolved publications after interruption and document manual pair recovery.
  Readers and other feeds that see a marker wait for its owning store lock and
  re-check, so an in-progress publication delays them instead of failing them.
  A busy or unusable lock during that wait raises `StoreReadError` naming the
  marker (the `StoreWriteError` is its `__cause__`).
* Retain both metadata backups when a refresh has uncertain publication durability.
* Retain selected enclosure media types across storage and use supported MIME
  extensions for extensionless/unrecognized URLs, preserving recognized suffixes
  and legacy records.
* Cover killed download owners and successor metadata/target revalidation with
  real CLI subprocesses and synthetic transfers.

* Report feed transport failures with private HTTP/timeout categories and no raw
  exception cause/context, including for library callers and formatted tracebacks.
* Refuse malformed/truncated feed XML before admitting partial episodes; retain
  tolerance for wrong MIME types, successfully repaired encoding declarations and
  recoverable entity errors (bare `&`, HTML entities) in documents that are
  otherwise well-formed under a strict re-parse.

* Bound advisory lock contention to 30 seconds per acquisition, configurable with
  `PDCST_LOCK_WAIT_SECONDS`; report busy/retry errors and close interrupted waits.
* Compare audio Content-Length against the byte budget before integer conversion,
  accepting leading zeros and refusing huge lengths with a private DownloadError.

* Refuse duplicate JSON object members at every nesting level before validating
  metadata, using private store-read errors and retaining the original bytes.
* Restrict replacement staging files to 0600 before writing payload, restoring
  existing or umask-filtered new-file permissions only for publication.

* Bound filename formatting width/precision before expansion and reject basenames
  over 255 UTF-8 bytes, retaining decimal index zero padding and standard
  fill/alignment, sign and grouping syntax. Newly refused: numbers above 255,
  the `c` integer type and string widths starting with `0` (which zero-fill on
  the right); edit such stored patterns in `podcasts.json` or re-add the podcast.
* Index normalized subscription directory ownership once under the writer lock
  so colliding-title suffix selection uses linear path-resolution work.

* Repair the pre-commit configuration: Flake8 now comes from its GitHub
  repository (the GitLab URL is gone), isort moves to the PyCQA repository, and
  all hooks are bumped to releases that install on current Python. Black's
  current style is applied to the sources and tests.

* Sync parent directories after replacement and before retiring previous audio;
  report post-replacement sync failures as uncertain durability while keeping
  visible audio/metadata coherent and retaining previous audio for inspection.
* Reject empty successful audio responses before publication, preserving prior
  files and download links.

* Refuse metadata symlinks without touching their targets. Admit only regular,
  stable no-follow descriptors for metadata, download and feed advisory locks;
  cover FIFO/link refusal and actual two-process serialization.

* Restore the previous episode generation if subscription publication fails;
  retain recovery generations when compensation also fails. A refresh whose
  episode directory sync fails but is fully rolled back reports a rolled-back
  write failure instead of a publication; the restored file keeps its original
  permissions, and a failed rollback-copy write leaves no recovery file.
* Report a directory-sync failure of the internal download stage as an ordinary
  download failure; nothing is published or reported as published.
* Refuse NFC/case-equivalent cross-feed directory ownership on every host.

* Keep successful downloads successful when previous-backup retirement fails;
  report the retained backup with a bounded cleanup warning.
* Preserve the initiating publication error when audio rollback also fails;
  retain both audio versions, report their recovery paths, and avoid overwriting
  completed bytes during incomplete restoration.

* Close the temporary descriptor when stream creation fails; cover atomic storage
  publication and cleanup with binary/text fault and interruption tests.
* Drop unusable refresh audio associations, including symlinks, loops, reserved
  names and conflicting filename/file ownership, while retaining all audio bytes.

* Require unique download title selections, prefer unique exact titles, and reject
  empty or ambiguous identifiers before any transfer or metadata write.
* Enforce the 16 MiB read ceiling on encoded metadata writes; preflight
  subscription size before episode import to preserve associations on refusal.

* Allow help and empty browsing before the data root exists; create it only for
  writing operations and report a configured file root as a concise CLI error.
  Report add's storage-creation/update filesystem failures without raw exceptions.
* Handle private categorized store-read errors in all commands, refusing writes
  against unreadable existing metadata. Handle filename-pattern errors in download.

* Bound regular metadata reads to 16 MiB and report private, categorized read errors.
* Restore prior audio on metadata publication failure and retain completed staging
  bytes for recovery; allow retry without manually moving the final destination.
* Validate filename template fields and format syntax before publication.

* Reload browse reads and create default repositories per call so subscriptions
  and updates from cooperating writers are visible without restarting.
* Report expected audio HTTP, transport and budget failures as private, bounded
  CLI errors, preserving old audio, metadata and completed-audio recovery behavior.
  The transfer helper raises DownloadError (a ValueError); the command function
  pdcst.download now converts these to typer.BadParameter, including budget errors
  previously exposed as ValueError.

* Refuse duplicate feed, incoming and stored identities before publication,
  including identical duplicates; preserve old JSON and audio on refusal.
* Serialize same-feed acquisition through publication with stable per-feed locks,
  allowing different feeds to acquire independently.

* Claim download destinations by episode GUID, refusing cross-episode collisions,
  unowned files, symlinks and repository filenames before transfer/publication.
  Ignore unsafe sibling associations, including symlink loops, when checking ownership.
* Commit only download associations against the current enclosure URL, preserving
  refreshed feed metadata and retaining completed conflicting audio for recovery.
  Drop old download associations when a feed changes the enclosure URL.

* Cover the registered subscription and episode browsing commands with isolated,
  offline CLI tests for output, show selection, podcast association and unchanged
  stored metadata; document their silent empty/no-match behavior.
* Preserve valid local download associations when refreshing subscriptions.
* Assign separate directories to distinct feeds with colliding titles and reject
  explicit cross-feed directory reuse.
* Report unmatched download selections as CLI errors; match titles case insensitively.
* Fetch feed bytes with status, size, elapsed-time and redirect bounds while
  preserving relative enclosures and HTTP date metadata.

* Accept feeds without HTTP Last-Modified timestamps. Prefer channel update/publication
  dates, then HTTP Last-Modified, then the newest usable episode date.
* Store feed and episode dates in UTC; interpret legacy naive stored dates as UTC.
* Bound the Pydantic dependency to the compatible 1.x API.
* Select supported audio enclosures, with a URL-extension fallback for untyped audio.
  Warn when unusable entries are skipped and reject feeds without usable episodes.
* Preserve existing subscription directories when sanitizing new feed names.
* Preserve file modes on atomic replacement and honor umask for new files.
* Contain feed-derived paths and validate directory and filename options.
* Publish successful complete HTTP downloads atomically; bound redirects.
* Lock JSON read-modify-write operations and atomically replace metadata.

# 2022-02-10 v0.12

* No pop of feed in Podcast.from_dict
* Some documentation updates

# 2022-02-09 v.011

* Improved name pattern for podcast episode audio files
* Some more tests
