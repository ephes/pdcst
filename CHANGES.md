# Unreleased

* Accept feeds without HTTP Last-Modified timestamps. Prefer channel update/publication
  dates, then HTTP Last-Modified, then the newest usable episode date.
* Store feed and episode dates in UTC; interpret legacy naive stored dates as UTC.
* Bound the Pydantic dependency to the compatible 1.x API.
* Select supported audio enclosures, with a URL-extension fallback for untyped audio.
  Warn when unusable entries are skipped and reject feeds without usable episodes.
* Contain feed-derived paths and validate directory and filename options.
* Publish successful complete HTTP downloads atomically; bound redirects.
* Lock JSON read-modify-write operations and atomically replace metadata.

# 2022-02-10 v0.12

* No pop of feed in Podcast.from_dict
* Some documentation updates

# 2022-02-09 v.011

* Improved name pattern for podcast episode audio files
* Some more tests
