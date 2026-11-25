# Development and release commands

Run these commands from the repository root. Use Python 3.10 or newer. Source builds use Cargo, Rust and a platform linker. Install a Rust toolchain with [rustup](https://rustup.rs/) or your operating system's package manager.

## Set up a development environment

```sh
python -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e .
```

On Windows PowerShell, activate with `.\.venv\Scripts\Activate.ps1`. The checkout install builds the private Rust extension through [Maturin](https://www.maturin.rs/). To rebuild it after editing Rust, reinstall in editable mode or use Maturin directly:

```sh
python -m pip install 'maturin>=1.7,<2'
maturin develop --release --locked
```

For a source-only Python run on POSIX, use `PYTHONPATH=src` in a clean checkout. On PowerShell, set `$env:PYTHONPATH = "src"` for the session. Native tests skip when the extension is absent.

## Run tests and the example

```sh
python -m unittest discover -s tests -v
cargo test --locked --manifest-path native/Cargo.toml --no-default-features
python examples/basic.py
python -c 'from chronodict import ChronoDict; assert ChronoDict(backend="native").backend == "native"'
```

For a focused run, select a test filename:

```sh
python -m unittest discover -s tests -p 'test_branching.py' -v
python -m unittest discover -s tests -p 'test_native_parity.py' -v
```

Use the native import check before counting a run as native verification. The standalone Cargo suite exercises the Rust engine preview directly. The Python suite exercises the public mapping and the extension when present.

## Benchmark and profile

The temporal comparison includes pandas when installed. The scale and profiling scripts use the POSIX `resource` module.

```sh
python -m pip install pandas
python benchmarks/benchmark_dict.py --size 5000 --repeats 5
python benchmarks/benchmark_collections.py --size 5000 --repeats 5 --history-size 100 --updates 100
python benchmarks/benchmark_temporal.py --size 5000 --updates 5000 --queries 100 --repeats 5
python benchmarks/benchmark_scale.py --sizes 10000 100000 --queries 30 --repeats 3
python benchmarks/profile_chronodict.py --mode cpu --size 10000 --queries 30 --repeats 1
python benchmarks/profile_chronodict.py --mode memory --size 10000 --queries 30 --repeats 1
```

The core package has no third-party runtime dependencies. pandas is only for the comparison.

## Build and verify release artifacts

With Maturin installed, the POSIX sequence below builds a source distribution and a wheel, then installs the wheel into a fresh environment. The artifacts and verification environment are placed in the printed temporary directory.

```sh
wheel_dir=$(mktemp -d)
maturin build --release --sdist --locked --compatibility pypi --out "$wheel_dir"
python -m venv "$wheel_dir/venv"
"$wheel_dir/venv/bin/python" -m pip install --no-index --no-deps "$wheel_dir"/chronodict-*.whl
"$wheel_dir/venv/bin/python" -I -c 'from chronodict import ChronoDict; assert ChronoDict(backend="native").backend == "native"'
"$wheel_dir/venv/bin/python" -I -m unittest discover -s tests
printf 'Release test directory: %s\n' "$wheel_dir"
```

On Windows use the equivalent `Scripts\python.exe` paths and pass the wheel filename directly. Build and clean-install the wheel for each platform, architecture, and Python ABI you intend to publish. The default build targets the selected local interpreter. Linux release wheels need an appropriate manylinux or musllinux build environment. [Maturin distribution guide](https://www.maturin.rs/distribution)

Before uploading, confirm the package name and version, check the artifacts and metadata, and verify the security reporting route. The source archive and platform wheels all use the `chronodict` distribution name. Use [the Python Packaging User Guide](https://packaging.python.org/en/latest/tutorials/packaging-projects/) for the upload workflow.
