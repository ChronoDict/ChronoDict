# Frequently asked questions

## How do I install ChronoDict?

Install a wheel that matches your Python interpreter and platform, or run `python -m pip install -e .` from a checkout with Rust and Cargo available. One package supplies both backends. [Commands](COMMANDS.md)

## Why does `backend="native"` raise `ImportError`?

The current interpreter could not load the bundled extension. Activate the environment where you installed ChronoDict, or rebuild from the checkout. Confirm with `python -c 'from chronodict import ChronoDict; print(ChronoDict(backend="native").backend)'`. The Python backend is available from the source tree as well. [Build commands](COMMANDS.md#set-up-a-development-environment)

## Why did editing a value from `get()` have no effect?

`get()` returns a detached copy. Use assignment, `patch()`, or bracket access to a tracked dictionary or list. If a tracked view raises a stale-view error, retrieve it again from the current mapping and reapply your edit. [Nested mutation examples](SYNTAX.md#edit-nested-data)

## Why did an indexed write or range query raise `TypeError`?

Indexed field values and indexed equality targets must be hashable. For ranges, use an exact `int`, `float`, `str`, or `bytes` target, excluding booleans and NaN. A range compares values within one domain. Missing fields and incompatible field values are skipped. [Query syntax](SYNTAX.md#find-records-by-field)

## Why are recent and older queries returning different results?

A revision or timestamp selects the state visible at that point. For `find_changed()`, the record must also have a write inside the specified interval. The filters are applied to the end-state value. [Historical reads](API.md#historical-reads-and-rollback)

## Can I recover an expired revision?

Retention discards expired history. Set limits according to how long you need to query or roll back. For recovery after process exit, store any snapshots you need separately using a format suitable for your values. [Retention syntax](SYNTAX.md#limit-retained-history)

## Why did a transaction or branch merge conflict?

An optimistic transaction conflicts when the owner revision changed before publication. Re-run the transaction against the latest state. A merge reports conflicting keys through `MergeConflictError.keys`, so you can review the endpoints with `compare()` and reconcile the conflicting values. [Transaction and branch API](API.md#transactions-branching-and-retention)

## Where should I expect indexing to help?

Selective field queries can visit fewer candidates when the fields are indexed. Maintaining the index adds work to writes and uses memory. Check your query selectivity, history depth, and retention settings with a representative workload. [Benchmark commands](COMMANDS.md#benchmark-and-profile)
