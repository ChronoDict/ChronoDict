"""In-memory versioned mapping with temporal equality indexes."""
from bisect import bisect_right
from collections.abc import MutableMapping, Mapping
from copy import deepcopy
from threading import RLock
from datetime import datetime, timezone

_MISSING = object()


class ChronoDict(MutableMapping):
    """Revisioned mapping. Values are copied on input and output to prevent untracked mutations."""

    def __init__(self, data=None, *, indexes=()):
        self._lock = RLock()
        self._data = {}
        self._history = {}
        self._indexes = {field: {} for field in indexes}
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

    def find(self, *, version=None, **conditions):
        """Intersect temporal equality indexes; scan only for unindexed conditions."""
        with self._lock:
            revision = self._validate_revision(version)
            candidates = None
            for field, target in conditions.items():
                if field not in self._indexes:
                    continue
                matches = set()
                for key, intervals in self._indexes[field].get(target, {}).items():
                    pos = bisect_right(intervals, revision, key=lambda interval: interval[0]) - 1
                    if pos >= 0:
                        end = intervals[pos][1]
                        if end is None or revision < end:
                            matches.add(key)
                candidates = matches if candidates is None else candidates & matches
            keys = self._history if candidates is None else candidates
            result = {}
            for key in keys:
                value = self._at(key, revision)
                if value is _MISSING:
                    continue
                if not conditions or (isinstance(value, Mapping) and all(
                        field in value and value[field] == target for field, target in conditions.items())):
                    result[key] = deepcopy(value)
            return result

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
