"""Shared core of the data-analyst skill: keys, environment, HTTP, routes, cache, packaging, manual
downloads, policy enforcement and the declarative REST adapter. Source packs (../../sources/<id>/) build
on it; see docs/pack_contract.md in the data-analyst repo."""

import csv as _csv

# Python's csv module stops at 128 KB per field by default ("field larger than field limit"). Agency CSVs can
# carry far larger cells (map geometries, full-text documents, JSON blobs), so every reader that imports core
# (fetch, packaging, audits) accepts them. 2**31 - 1 is the largest value a C long holds on every platform.
_csv.field_size_limit(2**31 - 1)
