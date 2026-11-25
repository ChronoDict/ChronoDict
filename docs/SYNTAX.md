# ChronoDict syntax

## Create and use a mapping

Pass a mapping or an iterable of key/value pairs. Each initial assignment gets its own revision. Keys and indexed field values must be hashable and keep stable hash and equality behavior. Stored values must support `copy.deepcopy`.

```python
from chronodict import ChronoDict

jobs = ChronoDict([("sync", {"status": "queued"}), ("index", {"status": "ready"})])
assert jobs.revision == 2
assert list(jobs) == ["sync", "index"]
assert jobs.get("unknown", "missing") == "missing"

jobs["sync"] = {"status": "running"}
jobs.setdefault("cleanup", {"status": "queued"})
jobs.update({"index": {"status": "running"}})
assert jobs.pop("cleanup") == {"status": "queued"}
del jobs["sync"]
assert "sync" not in jobs
```

Use `len(data)`, `key in data`, iteration, `keys()`, `items()`, and `values()` for standard mapping access. `popitem()` removes the last inserted current key. Top-level `update()` and `clear()` perform individual mapping writes. Wrap a batch in `transaction()` when it must publish together.

## Edit nested data

Bracket access tracks ordinary dictionaries and lists, including nested containers reached by indexing. A mutation writes a new revision for the root record. `get()`, `copy()` on a tracked view, list slices, and list iteration provide detached data. Set `track_mutations=False` to get detached values with bracket access too.

```python
from chronodict import ChronoDict

jobs = ChronoDict({"sync": {"status": "queued", "tags": []}}, indexes=["status"])
jobs["sync"]["tags"].append("nightly")
jobs.patch("sync", status="ready")
assert jobs.get("sync", version=1) == {"status": "queued", "tags": []}
assert jobs.get("sync") == {"status": "ready", "tags": ["nightly"]}

detached = jobs.get("sync")
detached["tags"].append("local")
assert jobs.get("sync")["tags"] == ["nightly"]
```

A view is tied to the revision of its key. Reacquire it after another write changes that key. `patch(key, **changes)` replaces top-level fields of a dictionary. It leaves fields outside the patch in place.

## Find records by field

`find()` combines conditions with AND. A plain field name means equality. Use `__gt`, `__gte`, `__lt`, and `__lte` for ranges. Range targets are exact `int`, `float`, `str`, or `bytes` values, with NaN excluded. Numbers, text, and bytes form separate comparison domains. A missing field or incompatible domain fails a range predicate.

```python
from chronodict import ChronoDict

inventory = ChronoDict(indexes=["warehouse", "units"])
inventory["usb"] = {"warehouse": "east", "units": 20, "state": "active"}
inventory["dock"] = {"warehouse": "east", "units": 3, "state": "active"}
inventory["cable"] = {"warehouse": "west", "units": 12, "state": "held"}
inventory.create_index("state")
inventory.create_composite_index("warehouse", "state")

assert set(inventory.find(warehouse="east", units__gte=10)) == {"usb"}
assert set(inventory.find(warehouse="east", state="active")) == {"usb", "dock"}
assert inventory.indexed_fields == ("warehouse", "units", "state")
assert inventory.composite_indexes == (("state", "warehouse"),)

inventory.drop_composite_index("warehouse", "state")
inventory.drop_index("state")
assert set(inventory.find(warehouse="east", state="active")) == {"usb", "dock"}
```

Single-field indexes support equality and scalar ranges. Composite indexes support equality when all indexed fields appear in the query. Unindexed conditions still work via scanning or filtering indexed candidates. An explicitly indexed name such as `score__gt` is interpreted as a literal field. Result order is unspecified.

## Read history and restore a value

`get()`, `find()`, `snapshot()`, and `rollback()` accept a revision number or a timezone-aware timestamp. Get a revision's UTC timestamp with `timestamp()`. Use `revision_at()` for the latest revision at or before a timestamp. Ties select the latest revision.

```python
from chronodict import ChronoDict

settings = ChronoDict({"search": {"enabled": False}})
baseline = settings.revision
settings.patch("search", enabled=True)
changed = settings.revision
assert settings.get("search", version=baseline) == {"enabled": False}
assert settings.get("search", at=settings.timestamp()) == {"enabled": True}
assert settings.revision_at(settings.timestamp(changed)) == changed
assert settings.diff("search", from_version=baseline, to_version=changed) == {
    "before_exists": True,
    "after_exists": True,
    "before": {"enabled": False},
    "after": {"enabled": True},
}
settings.rollback("search", version=baseline)
assert settings.snapshot() == {"search": {"enabled": False}}
assert [entry["version"] for entry in settings.history("search")] == [1, 2, 3]
```

`history(key)` includes tombstones for retained deletions. `deleted` distinguishes a deletion from a stored `None`. `diff()` similarly exposes presence flags.

## Run a transaction or an atomic update

Write through the stage yielded by `transaction()`. The parent exposes its committed data until the block succeeds. An unsuccessful block leaves the parent revision unchanged. The stage has its own revision numbering, starting at zero with the parent's initial state.

```python
from chronodict import ChronoDict

stock = ChronoDict({"east": {"units": 10}, "west": {"units": 3}}, indexes=["units"])
start = stock.revision
with stock.transaction() as stage:
    stage.patch("east", units=8)
    stage.patch("west", units=5)
    assert stock.get("east") == {"units": 10}

assert stock.revision == start + 1
assert set(stock.find_changed(start, stock.revision, units__gte=5)) == {"east", "west"}

def increment(current):
    return {"units": current["units"] + 1}

assert stock.atomic_update("east", increment) == {"units": 9}
```

Use `transaction(optimistic=True)` to allow concurrent owner writes. A commit conflicts if the owner's revision advanced, even when the other write touched a different key. Retry the complete transaction in a new context when that fits your application.

## Fork a configuration and review the changes

`branch(name)` forks the current revision. Use `version=` or `at=` for a retained historical fork point. The returned mapping can be read, queried, and updated independently. `"main"` names the mapping on which you call `compare()`. Other names refer to its direct child branches.

```python
from chronodict import ChronoDict, MergeConflictError

config = ChronoDict({"router": {"enabled": False, "limit": 20}}, indexes=["enabled"])
baseline = config.revision
trial = config.branch("trial", version=baseline)
trial.patch("router", enabled=True, limit=40)
assert config.compare("main", "trial")["router"]["after"]["limit"] == 40

config.patch("router", limit=25)
try:
    config.merge("trial")
except MergeConflictError as error:
    assert error.keys == ("router",)
else:
    raise AssertionError("Expected a conflict on router")

trial["router"] = config.get("router")
assert config.merge("trial") == config.revision
trial.patch("router", enabled=True)
before_merge = config.revision
after_merge = config.merge("trial")
assert config.find_changed(before_merge, after_merge, enabled=True) == {
    "router": {"enabled": True, "limit": 25},
}
```

Competing writes conflict at the key level unless the resulting states agree. A successful merge publishes the source changes in one parent revision when there are writes to apply. Branch index and retention settings are local.

`find_changed(start, end, **conditions)` evaluates values at `end` and includes keys with any write in `(start, end]`. This includes repeated assignments and changes later reverted within the interval. Deleted keys appear only if they are present again at `end`. Use `history()` or `diff()` to inspect deletions.

## Limit retained history

```python
from datetime import timedelta

from chronodict import ChronoDict

config = ChronoDict(max_revisions=3, max_age=timedelta(hours=1))
for value in range(5):
    config["limit"] = value

assert config.oldest_revision >= 3
assert config.get("limit", version=config.oldest_revision) is not None
config.set_retention(max_age=None)
assert config.prune() == config.oldest_revision
assert config.compact()["oldest_revision"] == config.oldest_revision
```

Retention advances as writes occur. Call `prune()` after idle periods when using age retention. Expired revisions and timestamps raise `ValueError`. Turning a policy off or relaxing it preserves the current retention boundary.

## Select the backend

```python
from chronodict import ChronoDict

data = ChronoDict(indexes=["status"], backend="python")
assert data.backend == "python"
```

Use `backend="native"` with a build that includes the extension. Both backends use the same mapping and query syntax. [API reference](API.md) · [Build commands](COMMANDS.md)
