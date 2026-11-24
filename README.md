# ChronoDict

**A time-aware indexed dictionary for Python.**
**It supports automatically versioned temporal indexing, version-aware key-value.**

v1.0: an in-memory MutableMapping with revision history and temporal secondary equality indexes. No runtime dependencies.

## Getting started

Requires Python 3.10+. From the project root:

```sh
python -m pip install -e .
python -m unittest discover -s tests -v
python examples/basic.py
```

```python
from chronodict import ChronoDict

data = ChronoDict(indexes=['department'])
data[101] = {'department': 'IT', 'salary': 50000}
data.patch(101, salary=70000)
data.patch(101, department='Finance')
assert data.find(department='IT', version=1)[101]['salary'] == 50000
data.rollback(101, version=1)
```

## Contract

- Global monotonically increasing integer revisions; revision zero is empty. Each successful assignment or deletion creates a revision. Rollback writes a new revision when it changes the presence or assigns a value.
- `get(key, default=None, version=None)`, `find(version=None, **conditions)`, `snapshot(version=None)`, `history(key)`, `diff(key, from_version=..., to_version=...)`, `rollback(key, version=...)` and `patch(key, **changes)`.
- Values are copied on read and write. Use `patch` or reassignment rather than nested in-place mutation. Values must support deepcopy; keys and indexed field values must be stable hashable objects with normal equality semantics.
- Indexes support top-level equality conditions. Unindexed conditions use scanning or filter indexed candidates. Search result order is unspecified.
- Revision intervals use inclusive start and exclusive end. Deletion is represented by a tombstone. History timestamps are UTC metadata; revisions define ordering.
- Individual core operations use a reentrant lock; compound inherited mapping operations are not transactions. History and indexes remain in memory and grow without bounds.
- No claim of novelty, production readiness or PyPI name availability is made.

See `project-map.md` for planned work.
