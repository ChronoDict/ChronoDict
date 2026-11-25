# Contributing to ChronoDict

For a bug report, include the ChronoDict version or commit, Python version, operating system, selected backend, and a short example with expected and actual behavior. For a performance report, include the workload, history depth, index configuration, and benchmark command. Use the private reporting process for vulnerabilities. [Security policy](SECURITY.md)

## Work on a change

1. Create a branch from the branch that your pull request will target.
2. Set up an editable install from the checkout. [Development commands](docs/COMMANDS.md#set-up-a-development-environment)
3. Keep the patch focused. Match the surrounding Python or Rust style.
4. Cover changed behavior or a reproduced bug with a focused test. Check current and historical behavior when a change touches writes, indexes, or retention. Check both backends when a change crosses the Python–Rust boundary.
5. Update the relevant [syntax guide](docs/SYNTAX.md), [API reference](docs/API.md), or [changelog](CHANGELOG.md) when users will see a change.

## Verify changes

```sh
python -m unittest discover -s tests -v
cargo test --locked --manifest-path native/Cargo.toml --no-default-features
git diff --check
```

For native work, rebuild the extension and verify that the Python native tests ran. For documentation examples, run the snippets in a fresh interpreter. Include the commands and results in the pull request. For performance changes, report comparable before and after results with the same dataset and options. [Benchmark commands](docs/COMMANDS.md#benchmark-and-profile)

## Submit a pull request

Describe the problem, the change users will see, and any compatibility or performance tradeoffs. Link related issues when available. Keep discussion focused on the work and treat contributors respectfully.

ChronoDict is distributed under the [MIT license](LICENSE).
