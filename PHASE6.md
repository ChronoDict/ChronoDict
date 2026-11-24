# Phase 6 — Native engine development preview

The Phase 5.6 Python implementation remains the default public API. The native engine is a separate experimental package, not a drop-in replacement. It supports string keys and flat records of string field names with signed 64-bit integer values. It does not support arbitrary Python object keys/values, nested mutation tracking, timestamps, composite/dynamic indexes, retention, compaction, transactions, or optimistic concurrency. Native snapshots are sorted by key, not insertion ordered. Python's bool/int conversion may coerce bool to integer; exact Python type parity is not promised. Calls retain the GIL; the mutex serializes native access. No speedup is claimed.

## Phase scope

| Step | Delivered | Remaining |
|---|---|---|
| 6.1 | Cargo, Maturin and PyO3 configuration | Compile and verify toolchain |
| 6.2 | HashMap current store | Arbitrary Python key/value semantics |
| 6.3 | Delta records, checkpoints, tombstones, rollback | Full reference feature parity |
| 6.4 | Temporal equality and inclusive integer range indexes | Composite, dynamic and other scalar indexes |
| 6.5 | Arc ownership and mutex | Native profiling, retention and concurrency parity |
| 6.6 | Six Rust tests and six Python parity tests | Execute native tests and full parity migration |

## Build and test on macOS

From the extracted ChronoDict directory (use your existing Python 3.12 environment or create one):

```sh
brew install rust
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e . 'maturin>=1.7,<2'
cargo test --manifest-path native/Cargo.toml --no-default-features
(cd native && maturin develop --release)
python -c 'from chronodict_native import NativeEngine; print(NativeEngine)'
python -m unittest discover -s tests -v
```

Expected after successful native build: 83 Python tests (77 existing plus six native parity tests), and six separate Cargo tests. An unbuilt native extension causes six Python tests to skip; this is not native validation. The build requires network access to fetch crates on first use. Cargo.lock is intentionally absent because dependency resolution could not be executed here; generate and commit it after successful build.

## Example

```python
from chronodict_native import NativeEngine

engine = NativeEngine(['score'])
engine.put('a', {'score': 10, 'fixed': 7})
engine.put('a', {'score': 20, 'fixed': 7})
assert engine.get('a', 1) == {'score': 10, 'fixed': 7}
assert engine.find('score', 20) == {'a': {'score': 20, 'fixed': 7}}
```

## Verification performed

Python 3.13.5 Linux: restored reference suite ran 77 tests, all passed. Combined discovery ran 83 tests with six skipped (native extension unavailable), and no failures. Native compiler, Cargo and Maturin were absent, and the environment could not resolve the Rust download host. Rust compilation and native test execution were not performed. The million-entry macOS lookup anomaly from Phase 5 remains open.
