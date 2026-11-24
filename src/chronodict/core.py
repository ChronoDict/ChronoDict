"""In-memory versioned mapping with temporal equality and range indexes."""
from bisect import bisect_left, bisect_right
from collections.abc import MutableMapping, Mapping
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


class ChronoDict(MutableMapping):
    """Revisioned mapping. Values are copied on input and output to prevent untracked mutations."""

    def __init__(self, data=None, *, indexes=()):
        self._lock = RLock()
        self._data = {}
        self._history = {}
        self._indexes = {field: {} for field in indexes}
        self._range_indexes = {field: {} for field in self._indexes}
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
            return deepcopy(self._data[key])

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
            value = self[key]
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
                if expression in self._indexes or not sep or op not in _RANGE_OPS:
                    field, op = expression, None
                elif _ordered_value(target) is None:
                    raise TypeError('Unsupported range target')
                predicates.append((field, op, target))
                if field not in self._indexes:
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
