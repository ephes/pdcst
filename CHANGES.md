# Unreleased

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
