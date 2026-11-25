# ChronoDict

ChronoDict is an in-memory Python mapping that automatically versions writes and maintains indexes across revisions. You can query past states by revision or timestamp, find records that changed between revisions, and try changes on isolated branches before merging them. It also supports atomic transactions and configurable history retention.

ChronoDict requires Python 3.10 or later. The default backend is Python, with optional Rust acceleration for indexed lookups.

## Documentation

[Installation and commands](docs/COMMANDS.md) · [Syntax and examples](docs/SYNTAX.md) · [API reference](docs/API.md)

[Architecture](docs/ARCHITECTURE.md) · [FAQ](docs/FAQ.md)

[Contributing](CONTRIBUTING.md) · [Changelog](CHANGELOG.md) · [Security](SECURITY.md) · [Roadmap](project-map.md) · [MIT license](LICENSE)
