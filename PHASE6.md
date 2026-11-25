# Phase 6 — Native engine development preview

The Phase 6 `NativeEngine` prototype supports string keys and flat records with string field names and signed 64-bit integer values. Its snapshots are sorted by key, and Python's bool/int conversion may coerce booleans to integers. Calls retain the GIL and a mutex serializes native access. Phase 7 bundles Rust in the `chronodict` distribution. The public entry point is `ChronoDict`, with Python as the default backend.

## Phase scope

| Step | Delivered | Remaining |
|---|---|---|
| 6.1 | Cargo, Maturin and PyO3 configuration with a verified macOS ARM64 build | Additional platform verification |
| 6.2 | HashMap current store | Arbitrary Python key/value semantics |
| 6.3 | Delta records, checkpoints, tombstones, rollback | Full reference feature parity |
| 6.4 | Temporal equality and inclusive integer range indexes | Composite, dynamic and other scalar indexes |
| 6.5 | Arc ownership and mutex | Native profiling, retention and concurrency parity |
| 6.6 | Six standalone Rust tests and six Python tests for `NativeEngine` | Full parity migration |

## Build and test on macOS

From the repository root, use your existing Python environment or create one.

```sh
brew install rust
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e .
cargo test --locked --manifest-path native/Cargo.toml --no-default-features
python -c 'from chronodict import ChronoDict; print(ChronoDict(backend="native").backend)'
python -m unittest discover -s tests -v
```

Native tests skip during source-only runs without a built extension. The repository includes `Cargo.lock`. Cargo may need network access to fetch dependencies on the first build. With Maturin installed, `maturin build --release --sdist --locked` builds the source archive and wheel for the same `chronodict` distribution.

## Example

```python
from chronodict import ChronoDict

data = ChronoDict(indexes=['score'], backend='native')
data['a'] = {'score': 10, 'fixed': 7}
data.patch('a', score=20)
assert data.get('a', version=1) == {'score': 10, 'fixed': 7}
assert data.find(score=20) == {'a': {'score': 20, 'fixed': 7}}
```

## Verification performed

On macOS ARM64 with Python 3.12, the Phase 6 audit built the native wheel, passed all 83 Python tests with the new wheel, and passed all six standalone Rust tests. The million-entry macOS lookup anomaly from Phase 5 remains open.
