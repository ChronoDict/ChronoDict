# Architecture

## Python and Rust

`chronodict.ChronoDict` is the public API. The default Python backend manages values, history, query planning, retention, transactions, and branches. The optional `backend="native"` path invokes `chronodict._native.active_posting_keys` to select active keys from temporal index entries. The query planner and result validation stay in Python. Rust uses PyO3 to read Python postings and return a set of matching keys. It holds the GIL while it does this work.

Maturin builds Python and the Rust extension into the same `chronodict` distribution. The Rust crate also contains a standalone string-key, integer-record engine that is exercised by the Cargo tests. That engine is a development preview. Its data model is separate from the public mapping's storage path.

## Values and revisions

- A hash table stores the current values. A current `get()` reads it directly. Historical point reads binary-search a key's revisions.
- Each key's history contains full checkpoints, sparse deltas where eligible and smaller, and deletion markers. Reconstructing a value starts at a checkpoint and replays a bounded number of deltas.
- Safe dictionary and list trees can share owned branches of data between revisions. Custom objects, aliases, and cycles follow the deepcopy path. Public historical reads and snapshots detach values from stored state.
- The revision counter orders writes. The UTC timestamp table is nondecreasing even when the system clock moves backward. Timestamp requests resolve to the latest revision at or before the requested time.

A tracked dictionary or list view carries the key's current revision. Mutating it edits a detached copy and commits the resulting root as one revision. A separate write can make the old view stale. [Mapping and mutation syntax](SYNTAX.md#edit-nested-data)

## Temporal indexes and queries

An equality posting maps a field value or composite tuple to keys and their revision intervals. Each interval includes its start and excludes its end. When an indexed field changes, the old interval closes and a new one opens. Single-field indexes also maintain sorted values for range comparisons. Composite indexes use equality.

`find()` resolves the target revision, selects candidates from eligible indexes, and checks all predicates against each candidate's value at that revision. Unindexed queries scan history keys. An indexed search can still visit keys that carried the value at earlier revisions, so selectivity across retained history matters. `find_changed()` adds a per-key history check for a write inside the requested interval.

## Retention

Retention advances the oldest accessible revision using the configured revision count and age limits. Expiry queues track superseded history entries and closed postings, so pruning works on affected entries as the boundary moves. Enabling retention on existing data schedules those entries once. A key that was untouched at the boundary keeps the value visible there. Age retention can be applied explicitly after idle periods with `prune()`. `compact()` also repacks retained histories.

## Transactions and branches

Transactions stage writes on a private mapping. Safe initial values can be shared, and the stage can defer index construction until a filtered query needs it. Commit prepares a candidate state, including copies of the affected histories and index buckets. The owner publishes the complete candidate in one revision under its lock. Exclusive transactions reject competing owner writes. Optimistic transactions accept staging alongside other writes and validate the owner's global revision before publishing.

A branch keeps the retained prefix through the fork point and has its own timeline after that. Forking copies history metadata and rebuilds indexes. Safe payloads can be shared, and values requiring isolation are copied. The parent keeps named branches registered after merge. It compares per-key revision checkpoints to detect competing edits during merge. Applying a merge uses the transaction publish path. Merge checkpoints let later merges incorporate further branch changes even when the original fork revision has expired.

## Scope

ChronoDict stores data in process memory. Its locks coordinate access within a process. The mapping provides versioned state, secondary indexes, and atomic publication in memory. [API contract](API.md)
