# Changelog

## Unreleased

- Reject metadata changed in place during reads while preserving coherent atomic
  replacement reads.
- Require strict positive stored episode indices and nonnegative integer counts.
- Persist refresh/audio publication intents before replacing generations; refuse
  unresolved publications after interruption and document manual pair recovery.
- Retain both metadata backups when a refresh has uncertain publication durability.
- Retain selected enclosure media types across storage and use supported MIME
  extensions for extensionless/unrecognized URLs, preserving recognized suffixes
  and legacy records.
- Cover killed download owners and successor metadata/target revalidation with
  real CLI subprocesses and synthetic transfers.
