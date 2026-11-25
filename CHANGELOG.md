# Changelog

## 0.1.0 (unreleased)

### Added

- An in-memory Python mapping with revision and timestamp reads, snapshots, per-key history, diffs, and rollback.
- Historical equality, scalar range, and composite equality indexes, with runtime index creation and removal.
- Tracked dictionary and list mutations, top-level patches, and atomic single-key updates.
- Exclusive and optimistic transactions with single-revision publication.
- Configurable revision-count and age retention, pruning, and compaction.
- Independent named branches, head comparison, atomic merges with per-key conflicts, and `find_changed()`.
- A single `chronodict` distribution with an opt-in Rust index lookup path.
- Developer guides for installation, syntax, the public API, architecture, benchmarking, and contribution.

### Performance and correctness

- Current `get()` reads the current-value table directly. Flat-record writes use a specialized path.
- Transaction stages share safe base values and can defer index construction. Commit copies affected histories and index buckets.
- Incremental retention prunes superseded history records and closed postings as the retention boundary advances.
- Tracked list `+=` records one revision for top-level and nested lists.

[API reference](docs/API.md) · [Benchmark commands](docs/COMMANDS.md#benchmark-and-profile)
