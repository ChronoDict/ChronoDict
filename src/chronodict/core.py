"""In-memory versioned mapping with temporal equality, range and composite indexes."""
from bisect import bisect_left, bisect_right
from collections.abc import MutableMapping, Mapping, MutableSequence
from operator import gt, ge, lt, le
from copy import deepcopy
from threading import RLock
from datetime import datetime, timezone

_MISSING = object()
_RANGE_OPS = {"gt": gt, "gte": ge, "lt": lt, "lte": le}


def _ordered_value(value):
    """Return a sortable domain for supported scalar range values."""
    if type(value) in (int, float) and value == value:
        return "number"
    if type(value) is str:
        return "string"
    if type(value) is bytes:
        return "bytes"
    return None


class _Tracked:
    """A revision-bound live view; stale writes are rejected, never rebased."""

    def __init__(self, owner, key, path, revision):
        self._owner = owner
        self._key = key
        self._path = path
        self._revision = revision

    def _node(self, root):
        for part in self._path:
            root = root[part]
        return root

    def _root(self):
        records = self._owner._history.get(self._key, ())
        if not records or records[-1][0] != self._revision or self._key not in self._owner._data:
            raise RuntimeError('Stale ChronoDict view; retrieve the key again')
        return self._owner._data[self._key]

    def _read(self):
        with self._owner._lock:
            return deepcopy(self._node(self._root()))

    def _change(self, operation):
        with self._owner._lock:
            root = deepcopy(self._root())
            result = operation(self._node(root))
            self._root()  # Reject reentrant changes during callbacks.
            self._owner._commit(self._key, root)
            self._revision = self._owner._revision
            return result

    def _child(self, part):
        with self._owner._lock:
            value = self._node(self._root())[part]
            cls = _TrackedDict if type(value) is dict else _TrackedList if type(value) is list else None
            if cls is not None:
                return cls(self._owner, self._key, self._path + (part,), self._revision)
            return deepcopy(value)

    def __deepcopy__(self, memo):
        return deepcopy(self._read(), memo)

    def __repr__(self):
        return repr(self._read())

    def __eq__(self, other):
        return self._read() == (other._read() if isinstance(other, _Tracked) else other)

    def __len__(self):
        return len(self._read())

    def __getitem__(self, key):
        return self._child(key)

    def __setitem__(self, key, value):
        self._change(lambda node: node.__setitem__(key, deepcopy(value)))

    def __delitem__(self, key):
        self._change(lambda node: node.__delitem__(key))


class _TrackedDict(_Tracked, MutableMapping):
    """Dictionary view with atomic mutation methods."""

    def __iter__(self):
        return iter(self._read())

    def update(self, *args, **kwargs):
        changes = deepcopy(dict(*args, **kwargs))
        self._change(lambda node: node.update(changes))

    def clear(self):
        self._change(lambda node: node.clear())

    def pop(self, key, *default):
        return self._change(lambda node: node.pop(key, *default))

    def popitem(self):
        return self._change(lambda node: node.popitem())

    def setdefault(self, key, default=None):
        with self._owner._lock:
            if key not in self._node(self._root()):
                self[key] = default
            return self[key]

    def copy(self):
        return self._read()


class _TrackedList(_Tracked, MutableSequence):
    """List view with atomic mutation methods; slices return detached copies."""

    def __getitem__(self, key):
        if isinstance(key, slice):
            return self._read()[key]
        return self._child(key)

    def __iter__(self):
        return iter(self._read())

    def insert(self, index, value):
        self._change(lambda node: node.insert(index, deepcopy(value)))

    def append(self, value):
        self._change(lambda node: node.append(deepcopy(value)))

    def extend(self, values):
        values = deepcopy(list(values))
        self._change(lambda node: node.extend(values))

    def pop(self, index=-1):
        return self._change(lambda node: node.pop(index))

    def remove(self, value):
        self._change(lambda node: node.remove(value))

    def clear(self):
        self._change(lambda node: node.clear())

    def reverse(self):
        self._change(lambda node: node.reverse())

    def sort(self, *, key=None, reverse=False):
        self._change(lambda node: node.sort(key=key, reverse=reverse))

    def copy(self):
        return self._read()


class ChronoDict(MutableMapping):
    """Revisioned mapping with optional live nested dict/list mutation tracking."""

    def __init__(self, data=None, *, indexes=(), track_mutations=True):
        self._lock = RLock()
        self._track_mutations = track_mutations
        self._data = {}
        self._history = {}
        self._indexes = {field: {} for field in indexes}
        self._range_indexes = {field: {} for field in self._indexes}
        self._composite_indexes = {}
        self._revision = 0
        self._times = [datetime.now(timezone.utc)]
        if data is not None:
            self.update(data)

    @property
    def revision(self):
        with self._lock:
            return self._revision

    def __len__(self):
        with self._lock:
            return len(self._data)

    def __iter__(self):
        with self._lock:
            return iter(tuple(self._data))

    def __getitem__(self, key):
        with self._lock:
            value = self._data[key]
            if self._track_mutations:
                cls = _TrackedDict if type(value) is dict else _TrackedList if type(value) is list else None
                if cls is not None:
                    return cls(self, key, (), self._history[key][-1][0])
            return deepcopy(value)

    @staticmethod
    def _field(value, field):
        if isinstance(value, Mapping) and field in value:
            result = value[field]
            hash(result)  # indexed attributes must be hashable
            return result
        return _MISSING

    def _commit(self, key, value, deleted=False):
        hash(key)
        with self._lock:
            if deleted and key not in self._data:
                raise KeyError(key)
            old = self._data.get(key, _MISSING)
            new = _MISSING if deleted else deepcopy(value)
            # Validate and prepare before changing state.
            transitions = [(field, self._field(old, field), self._field(new, field))
                           for field in self._indexes]
            composite_transitions = [(fields, self._composite_value(old, fields),
                                      self._composite_value(new, fields))
                                     for fields in self._composite_indexes]
            revision = self._revision + 1
            for field, before, after in transitions:
                if after is not _MISSING:
                    self._add_range_value(self._range_indexes[field], after)
                if before == after:
                    continue
                index = self._indexes[field]
                if before is not _MISSING:
                    index[before][key][-1][1] = revision
                if after is not _MISSING:
                    index.setdefault(after, {}).setdefault(key, []).append([revision, None])
            for fields, before, after in composite_transitions:
                self._transition(self._composite_indexes[fields], key, revision, before, after)
            self._history.setdefault(key, []).append((revision, new))
            if deleted:
                del self._data[key]
            else:
                self._data[key] = new
            self._revision = revision
            self._times.append(datetime.now(timezone.utc))

    def __setitem__(self, key, value):
        self._commit(key, value)

    def __delitem__(self, key):
        self._commit(key, None, deleted=True)

    def pop(self, key, *default):
        if len(default) > 1:
            raise TypeError('pop expected at most two arguments')
        with self._lock:
            if key not in self._data:
                if default:
                    return default[0]
                raise KeyError(key)
            value = deepcopy(self._data[key])
            del self[key]
            return value

    def popitem(self):
        with self._lock:
            if not self._data:
                raise KeyError('ChronoDict is empty')
            key = next(reversed(self._data))
            return key, self.pop(key)

    def _validate_revision(self, revision):
        if revision is None:
            return self._revision
        if type(revision) is not int or not 0 <= revision <= self._revision:
            raise ValueError('Invalid revision')
        return revision

    def _at(self, key, revision):
        records = self._history.get(key, ())
        pos = bisect_right(records, revision, key=lambda record: record[0]) - 1
        return records[pos][1] if pos >= 0 else _MISSING

    def get(self, key, default=None, *, version=None):
        with self._lock:
            revision = self._validate_revision(version)
            value = self._at(key, revision)
            return default if value is _MISSING else deepcopy(value)

    def patch(self, key, **changes):
        """Apply top-level field changes as one revision."""
        with self._lock:
            value = deepcopy(self._data[key])
            if not isinstance(value, dict):
                raise TypeError('patch requires a dict value')
            value.update(changes)
            self[key] = value

    def history(self, key):
        with self._lock:
            return [{'version': r, 'timestamp': self._times[r], 'deleted': v is _MISSING,
                     'value': None if v is _MISSING else deepcopy(v)}
                    for r, v in self._history.get(key, ())]

    def snapshot(self, version=None):
        with self._lock:
            revision = self._validate_revision(version)
            return {k: deepcopy(v) for k in self._history
                    if (v := self._at(k, revision)) is not _MISSING}

    @staticmethod
    def _add_range_value(groups, value):
        """Maintain sorted distinct historical values per comparable domain."""
        domain = _ordered_value(value)
        if domain is None:
            return
        values = groups.setdefault(domain, [])
        pos = bisect_left(values, value)
        if pos == len(values) or values[pos] != value:
            values.insert(pos, value)

    @staticmethod
    def _range_values(groups, target, op):
        domain = _ordered_value(target)
        if domain is None:
            raise TypeError('Range targets must be numbers, strings or bytes (excluding NaN)')
        values = groups.get(domain, ())
        if op == 'gt':
            return values[bisect_right(values, target):]
        if op == 'gte':
            return values[bisect_left(values, target):]
        if op == 'lt':
            return values[:bisect_left(values, target)]
        return values[:bisect_right(values, target)]

    @staticmethod
    def _active_keys(postings, revision):
        matches = set()
        for key, intervals in postings.items():
            pos = bisect_right(intervals, revision, key=lambda interval: interval[0]) - 1
            if pos >= 0:
                end = intervals[pos][1]
                if end is None or revision < end:
                    matches.add(key)
        return matches

    @staticmethod
    def _matches(value, field, op, target):
        if not isinstance(value, Mapping) or field not in value:
            return False
        actual = value[field]
        if op is None:
            return actual == target
        return (_ordered_value(actual) == _ordered_value(target)
                and _RANGE_OPS[op](actual, target))

    def find(self, *, version=None, **conditions):
        """Search current or historical values using equality and scalar ranges.

        Range suffixes: __gt, __gte, __lt, __lte. Incompatible scalar
        domains and missing fields do not match. Unindexed fields are scanned.
        An explicitly indexed literal field name takes precedence over a suffix.
        """
        with self._lock:
            revision = self._validate_revision(version)
            candidates = None
            predicates = []
            for expression, target in conditions.items():
                field, sep, op = expression.rpartition('__')
                if (expression in self._indexes or
                        any(expression in fields for fields in self._composite_indexes)
                        or not sep or op not in _RANGE_OPS):
                    field, op = expression, None
                elif _ordered_value(target) is None:
                    raise TypeError('Unsupported range target')
                predicates.append((field, op, target))
            equality = {field: target for field, op, target in predicates if op is None}
            covered = set()
            for fields, index in sorted(self._composite_indexes.items(),
                                        key=lambda item: -len(item[0])):
                if all(field in equality for field in fields) and not set(fields) <= covered:
                    target = tuple(equality[field] for field in fields)
                    matches = self._active_keys(index.get(target, {}), revision)
                    candidates = matches if candidates is None else candidates & matches
                    covered.update(fields)
            for field, op, target in predicates:
                if field not in self._indexes or (op is None and field in covered):
                    continue
                index = self._indexes[field]
                if op is None:
                    matches = self._active_keys(index.get(target, {}), revision)
                else:
                    matches = set()
                    for val in self._range_values(self._range_indexes[field], target, op):
                        matches.update(self._active_keys(index[val], revision))
                candidates = matches if candidates is None else candidates & matches
            keys = self._history if candidates is None else candidates
            result = {}
            for key in keys:
                value = self._at(key, revision)
                if value is not _MISSING and all(
                        self._matches(value, field, op, target)
                        for field, op, target in predicates):
                    result[key] = deepcopy(value)
            return result

    @property
    def indexed_fields(self):
        """Return indexed field names in creation order."""
        with self._lock:
            return tuple(self._indexes)

    def create_index(self, field):
        """Build an equality index across all retained revisions atomically.

        An unhashable historical field value rejects the entire operation.
        Index configuration does not create a data revision.
        """
        if not isinstance(field, str) or not field:
            raise ValueError('Index field must be a non-empty string')
        with self._lock:
            if field in self._indexes:
                raise ValueError(f'Index already exists: {field}')
            index = {}
            ranges = {}
            for key, records in self._history.items():
                before = _MISSING
                for revision, value in records:
                    after = self._field(value, field)
                    if after is not _MISSING:
                        self._add_range_value(ranges, after)
                    if before is after or (before is not _MISSING and
                                           after is not _MISSING and before == after):
                        continue
                    if before is not _MISSING:
                        index[before][key][-1][1] = revision
                    if after is not _MISSING:
                        index.setdefault(after, {}).setdefault(key, []).append([revision, None])
                    before = after
            self._indexes[field] = index
            self._range_indexes[field] = ranges

    def drop_index(self, field):
        """Remove an index without changing values or revision history."""
        with self._lock:
            del self._indexes[field]
            del self._range_indexes[field]

    @staticmethod
    def _normalize_composite(fields):
        """Canonicalize an unordered collection of distinct field names."""
        if len(fields) < 2 or any(not isinstance(f, str) or not f for f in fields):
            raise ValueError('A composite index requires at least two non-empty field names')
        if len(set(fields)) != len(fields):
            raise ValueError('Composite index fields must be distinct')
        return tuple(sorted(fields))

    @staticmethod
    def _composite_value(value, fields):
        """Return a hashable tuple only when every field is present."""
        if not isinstance(value, Mapping) or any(f not in value for f in fields):
            return _MISSING
        result = tuple(value[f] for f in fields)
        hash(result)
        return result

    @staticmethod
    def _transition(index, key, revision, before, after):
        """Close and open temporal postings when an indexed value changes."""
        if before is after or (before is not _MISSING and after is not _MISSING
                               and before == after):
            return
        if before is not _MISSING:
            index[before][key][-1][1] = revision
        if after is not _MISSING:
            index.setdefault(after, {}).setdefault(key, []).append([revision, None])

    @property
    def composite_indexes(self):
        """Return canonical field tuples in index creation order."""
        with self._lock:
            return tuple(self._composite_indexes)

    def create_composite_index(self, *fields):
        """Index complete field combinations across all retained revisions."""
        fields = self._normalize_composite(fields)
        with self._lock:
            if fields in self._composite_indexes:
                raise ValueError(f'Composite index already exists: {fields}')
            index = {}
            for key, records in self._history.items():
                before = _MISSING
                for revision, value in records:
                    after = self._composite_value(value, fields)
                    self._transition(index, key, revision, before, after)
                    before = after
            self._composite_indexes[fields] = index

    def drop_composite_index(self, *fields):
        """Remove a composite index without modifying data or history."""
        fields = self._normalize_composite(fields)
        with self._lock:
            del self._composite_indexes[fields]

    def rollback(self, key, *, version):
        """Restore a key at a revision, recording a new revision."""
        with self._lock:
            value = self._at(key, self._validate_revision(version))
            if value is _MISSING:
                if key in self._data:
                    del self[key]
            else:
                self[key] = value

    def diff(self, key, *, from_version, to_version):
        with self._lock:
            before = self._at(key, self._validate_revision(from_version))
            after = self._at(key, self._validate_revision(to_version))
            return {'before_exists': before is not _MISSING, 'after_exists': after is not _MISSING,
                    'before': None if before is _MISSING else deepcopy(before),
                    'after': None if after is _MISSING else deepcopy(after)}
