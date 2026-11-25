# ChronoDict API reference

```python
from chronodict import ChronoDict, MergeConflictError
```

The default backend is Python. Both backends expose this API. Keys and indexed field values must be hashable with stable equality and hashing. Values must support `copy.deepcopy`. Copy, comparison, custom mapping, and callback exceptions can propagate from user-supplied objects in addition to the common exceptions listed below.

## Construction and properties

```text
ChronoDict(
    data=None,
    *,
    indexes=(),
    track_mutations=True,
    max_revisions=None,
    max_age=None,
    backend="python",
)
```

| Parameter | Accepted value and behavior |
| --- | --- |
| `data` | A mapping, an iterable of key/value pairs, or `None`. Initial assignments go through `update()` and receive individual revisions. |
| `indexes` | Iterable of single-field index names. Supply non-empty strings. Each index is maintained on writes. |
| `track_mutations` | Set to `False` for detached bracket reads. The default enables tracked views for ordinary dictionaries and lists. |
| `max_revisions` | Positive `int` excluding `bool`, or `None`. Sets the maximum number of retained revision identifiers. |
| `max_age` | Positive `datetime.timedelta` or `None`. Retains the boundary state at or before the age cutoff. |
| `backend` | `"python"` or `"native"`. Both share Python storage and semantics. `"native"` also loads the bundled extension for index posting lookup. |

Construction returns a `ChronoDict`. Invalid retention values or backend names raise `ValueError`. Selecting an unavailable native backend raises `ImportError`. Malformed initial data and unhashable keys or indexed values raise the corresponding input or mapping exception, typically `TypeError` or `ValueError`.

| Read-only property | Type and meaning |
| --- | --- |
| `revision` | `int`. Current revision, starting at zero on a new empty mapping. |
| `oldest_revision` | `int`. Earliest revision that queries can access. |
| `backend` | `str`. Selected backend name. |
| `indexed_fields` | `tuple[str, ...]`. Single-field indexes in creation order. |
| `composite_indexes` | `tuple[tuple[str, ...], ...]`. Composite indexes in creation order, with fields sorted within each index. |

## Current mapping operations

| Operation | Returns or effect | Common exceptions |
| --- | --- | --- |
| `data[key]` | Current value or a tracked dictionary/list view. | `KeyError` for a missing key. |
| `data[key] = value` | Stores a copy in a new revision. | `TypeError` for an unhashable key or indexed field value. |
| `del data[key]` | Deletes a current key in a new revision. | `KeyError` for a missing key. |
| `key in data`, `len(data)`, `iter(data)` | Membership, count, and an iterator over current keys in insertion order. | `TypeError` for an unhashable membership key. |
| `keys()`, `items()`, `values()` | Standard mapping views. Values from these views follow bracket-access semantics. Use `snapshot()` for one detached revision. | Errors from evaluating a key or value can propagate. |
| `setdefault(key, default=None)` | Existing value, or inserts and returns `default` using bracket-access semantics. | Same as assignment. |
| `pop(key[, default])` | Removes the key and returns a detached value. For an absent key, returns the supplied default if present. | `KeyError` with no default for a missing key, or `TypeError` for multiple defaults. |
| `popitem()` | Removes and returns the last inserted current `(key, value)` pair, with a detached value. | `KeyError` when empty. |
| `update([data], **kwargs)` | `None`. Writes each entry individually using standard mapping update syntax. | Same as assignment. Invalid iterables can raise `TypeError` or `ValueError`. |
| `clear()` | `None`. Deletes current entries one at a time. | Same restrictions as deletion. |
| `patch(key, **changes)` | `None`. Applies the top-level field changes to an existing dictionary in one revision. | `KeyError` for a missing key, `TypeError` for a non-dictionary value or unhashable indexed field. |
| `atomic_update(key, transform)` | Passes a detached current value to `transform(value)`, commits its return value, and returns a detached copy of that replacement. | `KeyError` for a missing key, `TypeError` for a non-callable or invalid replacement, `RuntimeError` during an exclusive transaction or a reentrant atomic update. |

Direct owner writes during an exclusive transaction raise `RuntimeError`. A callback passed to `atomic_update()` runs while the mapping is locked. It can read the mapping. Keep it free of mapping mutations and cross-thread waits on the same mapping.

### Tracked dictionary and list views

With `track_mutations=True`, bracket access wraps exact `dict` and `list` values in revision-bound mapping and sequence views. Indexing into these views can return another tracked view. `copy()` and `copy.deepcopy(view)` return detached built-ins. List slicing and iteration also return detached data. A stale view raises `RuntimeError` on access. Reacquire the view after a separate write to the same record.

| View | Operations |
| --- | --- |
| Dictionary | Item get/set/delete, length, iteration, `get()`, `keys()`, `items()`, `values()`, `update([data], **kwargs)`, `setdefault(key, default=None)`, `pop(key[, default])`, `popitem()`, `clear()`, and `copy()`. |
| List | Integer and slice get/set/delete, length, iteration, `index(value[, start[, stop]])`, `count(value)`, `insert(index, value)`, `append(value)`, `extend(iterable)`, `+=`, `pop(index=-1)`, `remove(value)`, `clear()`, `reverse()`, `sort(*, key=None, reverse=False)`, and `copy()`. |

Normal mapping and sequence errors apply, such as `KeyError`, `IndexError`, `ValueError`, and `TypeError`. A successful mutator writes a new revision for the root record. `setdefault()` on an existing field is a read.

## Historical reads and rollback

A revision must be an exact `int` between `oldest_revision` and `revision`, inclusive. An omitted read endpoint defaults to the current revision. Time arguments must be timezone-aware `datetime.datetime` objects. Use a version or a timestamp at each endpoint. A pair of non-`None` values at the same endpoint raises `ValueError`. Invalid or expired revisions and naive or expired timestamps raise `ValueError`. A non-datetime timestamp raises `TypeError`.

| Method | Returns or effect |
| --- | --- |
| `get(key, default=None, *, version=None, at=None)` | Detached value at the requested revision, or the supplied default if absent. |
| `snapshot(version=None, *, at=None)` | Plain `dict` of all values visible at the selected revision, with detached values. |
| `history(key)` | List of records with `version: int`, `timestamp: datetime`, `deleted: bool`, and `value`, in revision order. Tombstones have `deleted=True` and `value=None`. Returns `[]` when the key has no retained history. |
| `timestamp(version=None)` | The timezone-aware UTC `datetime` for that revision. |
| `revision_at(timestamp)` | Latest revision at or before the timestamp. Equal commit times select the latest matching revision. Before construction, returns zero when that revision is still retained. |
| `diff(key, *, from_version=None, to_version=None, from_at=None, to_at=None)` | A detached dictionary with `before_exists`, `after_exists`, `before`, and `after`. Each endpoint requires a version or timestamp. |
| `rollback(key, *, version=None, at=None)` | `None`. Writes the selected historical value or removes the current key if the key was absent at the selected revision. If it was and remains absent, no write occurs. Requires an explicit endpoint. |

## Queries and indexes

| Method | Returns or effect | Common exceptions |
| --- | --- | --- |
| `find(*, version=None, at=None, **conditions)` | Plain `dict` of keys and detached values matching all top-level predicates. With no conditions, returns the visible state. | Endpoint errors above. `TypeError` for unsupported range targets or unhashable indexed equality targets. |
| `find_changed(from_version, to_version, /, **conditions)` | Same result form as `find()`, filtered to keys with a write in `(from_version, to_version]`. Conditions apply to values at `to_version`. Reverted writes count, and a key must be present at the end. | Same query validation. `ValueError` for `None`, invalid, expired, or reversed revisions. Omitted positional arguments raise `TypeError`. |
| `create_index(field)` | `None`. Adds a single-field equality and range index across retained revisions. | `ValueError` for invalid or duplicate field names, `TypeError` for an unhashable indexed value. |
| `drop_index(field)` | `None`. Removes the single-field index. | `KeyError` for an unknown field. |
| `create_composite_index(*fields)` | `None`. Adds an equality index for two or more distinct non-empty string fields. Field order is normalized. | `ValueError` for invalid fields or a duplicate index, `TypeError` for unhashable indexed values. |
| `drop_composite_index(*fields)` | `None`. Removes a composite index. | `ValueError` for invalid fields, `KeyError` for an unknown field combination. |

Conditions use equality or the suffixes `__gt`, `__gte`, `__lt`, and `__lte`. Range targets are exact `int`, `float`, `str`, or `bytes` values. Booleans and NaN are outside the range domains. Explicitly indexed literal field names take precedence over suffix parsing. Fields absent from a record do not match. Result ordering is unspecified. Index creation and removal leave data revisions unchanged.

## Transactions, branching, and retention

| Method | Returns or effect | Common exceptions |
| --- | --- | --- |
| `transaction(*, optimistic=False)` | Context manager yielding a private `ChronoDict` stage. Successful changes publish in one owner revision. An empty batch or a temporary key both created and removed in the stage leaves the owner revision unchanged. | `TypeError` if `optimistic` is not a `bool`. `RuntimeError` for nested or incompatible transactions, or an optimistic conflict. Stage write exceptions propagate. |
| `branch(name, *, version=None, at=None)` | Independent child `ChronoDict` containing retained history through the selected revision. | Endpoint errors above. `ValueError` for empty, reserved, or duplicate names. |
| `compare(left, right)` | `dict` keyed by differing keys, each mapped to the same before/after shape as `diff()`. Names are `"main"` for the caller or a direct child name. | `KeyError` for unknown names. |
| `merge(name)` | Parent revision `int` after merging the named child. Applies writes in one revision or returns the current revision when no writes are needed. | `KeyError` for an unknown child, `MergeConflictError` for conflicting keys, or a write validation error. |
| `set_retention(*, max_revisions=…, max_age=…)` | Oldest retained revision as `int` after applying the new settings. Omitted options keep their prior values. Pass `None` to disable one policy. | `ValueError` for invalid limits. |
| `prune()` | Oldest retained revision as `int` after applying the current policies to the present time. | Configuration restriction below. |
| `compact()` | `dict[str, int]` with `oldest_revision`, `records_before`, and `records_after`. Applies retention and repacks the remaining history. | Configuration restriction below. |

Index and retention configuration, branch creation, and merge are unavailable during an active transaction or atomic update and raise `RuntimeError`. Merge also requires the child to be available for configuration. A transaction stage has its own revision zero with the initial state. The optimistic mode rejects publication after any intervening owner revision.

`MergeConflictError` is a `RuntimeError` subclass. Its `keys` attribute is a tuple of keys with competing writes whose resulting states disagree. A failed merge leaves the parent and merge checkpoints intact.

[Syntax and walkthroughs](SYNTAX.md) · [Architecture](ARCHITECTURE.md)
