# pdcst

A small command line client for downloading podcast episodes.
It's in pre alpha status (just started a week ago).

# Installation

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

## Download

Download an episode which has "rest" in the title from the python podcast:

```shell
$ pdcst download python rest
```

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

## Storage and failure guarantees

`PDCST_HOME` selects the storage root. Feed-derived directory and episode titles
are converted to single safe components. `--podcast-dir` may specify a relative
subdirectory within that root; absolute paths, parent traversal, Windows paths,
and symlinks resolving outside the root are rejected before writing. Filename
patterns must produce a single relative filename. Existing subscriptions with
unsafe paths must be corrected before use. Containment assumes other local
processes do not change directory symlinks while a command is running.

Downloads require an HTTP success response and follow at most five redirects.
Missing or invalid Content-Length uses indeterminate progress. A valid length
must match the received bytes. Files are flushed into a sibling temporary file
and atomically replaced after completion; failures retain the previous file and
do not mark an episode downloaded. Filesystem errors propagate to the caller.

JSON repositories use stable `.json.lock` files and POSIX advisory locks to
serialize cooperating writers across reload and atomic replacement. Do not
remove lock files while commands are running. Failed serialization or writes
leave the previous JSON intact; leftover temporary files after a killed process
can be removed when no commands are running. Existing corrupt JSON must be
restored from backup; it is not silently overwritten. Mutations invalidate the
same instance's cached list; use a new instance to observe other writers.
