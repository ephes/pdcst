# Unreleased

* Claim download destinations by episode GUID, refusing cross-episode collisions,
  unowned files, symlinks and repository filenames before transfer/publication.
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
