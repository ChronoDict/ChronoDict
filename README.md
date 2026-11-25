# ChronoDict

ChronoDict is an in-memory Python mapping that automatically versions writes and maintains indexes across revisions. You can query past states by revision or timestamp, find records that changed between revisions, and try changes on isolated branches before merging them. It also supports atomic transactions and configurable history retention.

ChronoDict supports CPython 3.10 through 3.14 with the GIL enabled. The default backend is Python, with optional Rust acceleration for indexed lookups.

## Documentation

[Syntax and examples](https://github.com/ChronoDict/ChronoDict/blob/main/docs/SYNTAX.md) · [API reference](https://github.com/ChronoDict/ChronoDict/blob/main/docs/API.md)

[Architecture](https://github.com/ChronoDict/ChronoDict/blob/main/docs/ARCHITECTURE.md) · [Contributing](https://github.com/ChronoDict/ChronoDict/blob/main/CONTRIBUTING.md) ·

[License](https://github.com/ChronoDict/ChronoDict/blob/main/LICENSE)
