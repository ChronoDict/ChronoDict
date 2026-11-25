"""In-memory versioned mapping with temporal equality, range and composite indexes, with UTC timestamp retrieval conservative structural sharing, copy-efficient updates, bounded delta history configurable retention and history compaction, with atomic staged transactions."""
from contextlib import contextmanager
from bisect import bisect_left, bisect_right
from collections import deque
from collections.abc import MutableMapping, Mapping, MutableSequence
from operator import gt, ge, itemgetter, lt, le
from copy import deepcopy
from threading import RLock, get_ident
from sys import getsizeof
from datetime import datetime, timezone, timedelta
from itertools import chain, islice
from math import copysign
from weakref import ref

_MISSING = object()
_UNCHANGED = object()
# Per-key checkpoint cadence bounds historical replay to 31 deltas.
_CHECKPOINT_INTERVAL = 32
_MAX_NATIVE_REVISION = (1 << 64) - 1
_RANGE_OPS = {"gt": gt, "gte": ge, "lt": lt, "lte": le}


class MergeConflictError(RuntimeError):
    """Keys edited on both sides of a branch merge."""

    def __init__(self, keys):
        self.keys = tuple(keys)
        super().__init__(f'Merge conflict on {len(self.keys)} key(s)')


# Only exact built-in scalar leaves and unaliased dict/list trees are shared.
# Arbitrary objects, cycles and repeated container references retain deepcopy semantics.
_SHAREABLE_SCALARS = (type(None), bool, int, float, complex, str, bytes)


def _flat_scalar_dict(value):
    """Recognize exact dicts whose keys and values are immutable builtins."""
    if type(value) is not dict:
        return False
    for key, item in value.items():
        if type(key) not in _SHAREABLE_SCALARS or type(item) not in _SHAREABLE_SCALARS:
            return False
    return True


def _copy_value(value):
    """Detach simple values without recursive deepcopy; preserve all other semantics."""
    if type(value) in _SHAREABLE_SCALARS:
        return value
    if _flat_scalar_dict(value):
        return value.copy()
    return deepcopy(value)


def _plain_tree(value):
    """Check that a value has no custom objects or mutable alias topology."""
    seen = set()
    pending = [value]
    while pending:
        node = pending.pop()
        kind = type(node)
        if kind in _SHAREABLE_SCALARS:
            continue
        if kind not in (dict, list) or id(node) in seen:
            return False
        seen.add(id(node))
        if kind is dict:
            if any(type(key) not in _SHAREABLE_SCALARS for key in node):
                return False
            pending.extend(node.values())
        else:
            pending.extend(node)
    return True


def _share_tree(old, new):
    """Reuse unchanged branches of an owned, detached plain tree.

    Identity checks on leaves preserve distinctions such as signed zero,
    NaN and numeric types. Never mutate an earlier revision.
    """
    if old is new:
        return old
    if type(old) is not type(new):
        return new
    if type(new) is dict:
        same = len(old) == len(new)
        # Match only identical keys; preserve insertion order and key identity.
        previous = {id(key): value for key, value in old.items()}
        if same:
            same = all(a is b for a, b in zip(old, new))
        for key, value in new.items():
            prior = previous.get(id(key), _MISSING)
            shared = _share_tree(prior, value)
            new[key] = shared
            same = same and shared is prior
        return old if same else new
    if type(new) is list:
        same = len(old) == len(new)
        for i, value in enumerate(new):
            prior = old[i] if i < len(old) else _MISSING
            new[i] = _share_tree(prior, value)
            same = same and new[i] is prior
        return old if same else new
    return new


def _ordered_value(value):
    """Return a sortable domain for supported scalar range values."""
    if type(value) in (int, float) and value == value:
        return "number"
    if type(value) is str:
        return "string"
    if type(value) is bytes:
        return "bytes"
    return None


def _same_branch_value(before, after):
    """Compare values without exposing owned containers to custom equality."""
    if before is after:
        return True
    pending = [(before, after)]
    left_nodes = {}
    right_nodes = set()
    while pending:
        left, right = pending.pop()
        kind = type(left)
        if kind is not type(right) or ((left is _MISSING or right is _MISSING) and left is not right):
            return False
        if kind is dict or kind is list:
            left_id, right_id = id(left), id(right)
            if left_id in left_nodes:
                if left_nodes[left_id] != right_id:
                    return False
                continue
            if right_id in right_nodes or len(left) != len(right):
                return False
            left_nodes[left_id] = right_id
            right_nodes.add(right_id)
            if kind is dict:
                for (left_key, left_value), (right_key, right_value) in zip(left.items(), right.items()):
                    pending.extend(((left_key, right_key), (left_value, right_value)))
            else:
                pending.extend(zip(left, right))
        elif left is not right:
            if kind is complex:
                pending.extend(((left.real, right.real), (left.imag, right.imag)))
                continue
            if kind is float and left == right == 0 and copysign(1, left) != copysign(1, right):
                return False
            try:
                if not (_copy_value(left) == _copy_value(right)):
                    return False
            except (TypeError, ValueError, RecursionError):
                return False
    return True


def _describe_change(before, after):
    return {'before_exists': before is not _MISSING, 'after_exists': after is not _MISSING,
            'before': None if before is _MISSING else deepcopy(before),
            'after': None if after is _MISSING else deepcopy(after)}


class _Tracked:
    """A revision-bound live view; stale writes are rejected, never rebased."""

    def __init__(self, owner, key, path, revision, *, parent=None):
        self._owner = owner
        self._key = key
        self._path = path
        self._revision = revision
        self._parent = ref(parent) if parent is not None else None
        self._inplace_from = None

    def _node(self, root):
        for part in self._path:
            root = root[part]
        return root

    def _root(self):
        if (self._owner._key_revisions.get(self._key, 0) != self._revision
                or self._key not in self._owner._data):
            raise RuntimeError('Stale ChronoDict view; retrieve the key again')
        return self._owner._data[self._key]

    def _read(self):
        with self._owner._lock:
            return deepcopy(self._node(self._root()))

    def _change(self, operation, *, inplace=False):
        with self._owner._lock:
            root = deepcopy(self._root())
            result = operation(self._node(root))
            self._root()  # Reject reentrant changes during callbacks.
            self._owner._commit(self._key, root)
            self._inplace_from = self._revision if inplace else None
            self._revision = self._owner._revision
            return result

    def _consume_inplace_assignment(self, owner, key, path, parent=None):
        """Skip the assignment Python performs after an in-place list update."""
        if (self._inplace_from is None or self._owner is not owner
                or (self._key, self._path) != (key, path)):
            return False
        if parent is not None and (self._parent() is not parent
                                   or parent._revision != self._inplace_from):
            return False
        self._root()  # A competing write may have invalidated the updated view.
        if parent is not None:
            parent._revision = self._revision
        self._inplace_from = None
        return True

    def _child(self, part):
        with self._owner._lock:
            value = self._node(self._root())[part]
            cls = _TrackedDict if type(value) is dict else _TrackedList if type(value) is list else None
            if cls is not None:
                return cls(self._owner, self._key, self._path + (part,), self._revision, parent=self)
            return _copy_value(value)

    def __deepcopy__(self, memo):
        with self._owner._lock:
            return deepcopy(self._node(self._root()), memo)

    def __repr__(self):
        return repr(self._read())

    def __eq__(self, other):
        return self._read() == (other._read() if isinstance(other, _Tracked) else other)

    def __len__(self):
        with self._owner._lock:
            return len(self._node(self._root()))

    def __getitem__(self, key):
        return self._child(key)

    def __setitem__(self, key, value):
        if type(value) is _TrackedList:
            with self._owner._lock:
                if value._consume_inplace_assignment(self._owner, self._key,
                                                     self._path + (key,), self):
                    return
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
        self._extend(values)

    def __iadd__(self, values):
        self._extend(values, inplace=True)
        return self

    def _extend(self, values, *, inplace=False):
        values = deepcopy(list(values))
        self._change(lambda node: node.extend(values), inplace=inplace)

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


class _TransactionStage:
    """Key order at private revision zero and surviving staged insertions."""

    def __init__(self, base_keys, indexes_pending):
        self.base_keys = base_keys
        self.indexes_pending = indexes_pending
        self.inserted = {}


class _Branch:
    """A child mapping and the parent/child revision last shared for each key."""

    def __init__(self, mapping):
        self.mapping = mapping
        self.checkpoints = {key: (revision, revision) for key, revision in mapping._key_revisions.items()}

    def parent_revision(self, key):
        parent, local = self.checkpoints.get(key, (_MISSING, _MISSING))
        return parent if self.mapping._key_revisions.get(key, _MISSING) == local else _UNCHANGED


class _RetentionEvents:
    """Revision-ordered queues for superseded history and closed postings."""

    def __init__(self, fields=()):
        self.history = deque()
        self.indexes = {field: deque() for field in fields}
        self.composites = {}

    def copy(self):
        events = _RetentionEvents()
        events.history = self.history.copy()
        events.indexes = {field: pending.copy() for field, pending in self.indexes.items()}
        events.composites = {fields: pending.copy() for fields, pending in self.composites.items()}
        return events


class ChronoDict(MutableMapping):
    """Revisioned mapping with optional live nested dict/list mutation tracking."""

    def __init__(self, data=None, *, indexes=(), track_mutations=True,
                 max_revisions=None, max_age=None, backend='python'):
        if backend not in ('python', 'native'):
            raise ValueError("backend must be 'python' or 'native'")
        self._backend = backend
        self._native_posting_keys = None
        if backend == 'native':
            try:
                from ._native import active_posting_keys
            except ImportError as error:
                raise ImportError("backend='native' requires a ChronoDict build with Rust support") from error
            self._native_posting_keys = active_posting_keys
        self._lock = RLock()
        self._atomic_callback = False
        self._transaction_active = False
        self._optimistic_threads = set()
        self._stage = None
        self._transaction_source = None
        self._branches = {}
        self._track_mutations = track_mutations
        self._data = {}
        self._history = {}
        self._key_revisions = {}
        self._unshareable_keys = set()
        self._oldest_revision = 0
        self._validate_retention(max_revisions, max_age)
        self._max_revisions = max_revisions
        self._max_age = max_age
        self._indexes = {field: {} for field in indexes}
        self._range_indexes = {field: {} for field in self._indexes}
        self._composite_indexes = {}
        self._retention_events = (_RetentionEvents(self._indexes)
                                  if max_revisions is not None or max_age is not None else None)
        self._revision = 0
        self._times = [datetime.now(timezone.utc)]
        if data is not None:
            self.update(data)

    @staticmethod
    def _validate_retention(max_revisions, max_age):
        """Validate optional retained revision count and positive timedelta."""
        if max_revisions is not None and (type(max_revisions) is not int or max_revisions < 1):
            raise ValueError('max_revisions must be a positive integer or None')
        if max_age is not None and (not isinstance(max_age, timedelta) or max_age <= timedelta(0)):
            raise ValueError('max_age must be a positive timedelta or None')

    @property
    def backend(self):
        """Return the backend used for temporal index lookups."""
        return self._backend

    @property
    def oldest_revision(self):
        """Lowest accessible revision; revision identifiers never change."""
        with self._lock:
            return self._oldest_revision

    def set_retention(self, *, max_revisions=_UNCHANGED, max_age=_UNCHANGED):
        """Change policies and prune immediately; None disables a policy.

        Discarded history cannot be recovered by relaxing retention.
        """
        with self._lock:
            self._check_transaction_configuration()
            count = self._max_revisions if max_revisions is _UNCHANGED else max_revisions
            age = self._max_age if max_age is _UNCHANGED else max_age
            self._validate_retention(count, age)
            if count is None and age is None:
                self._retention_events = None
            elif self._retention_events is None:
                self._retention_events = self._schedule_retention()
            self._max_revisions, self._max_age = count, age
            return self.prune()

    def _schedule_retention(self):
        """Schedule expiry for existing state when retention is enabled."""
        pending = _RetentionEvents()
        changes = ((r, key) for key, records in self._history.items() if len(records) > 1
                   for r, _, _ in islice(records, 1, None))
        pending.history = deque(sorted(changes, key=itemgetter(0)))
        pending.indexes = {field: self._closed_intervals(index) for field, index in self._indexes.items()}
        pending.composites = {fields: self._closed_intervals(index)
                              for fields, index in self._composite_indexes.items()}
        return pending

    @staticmethod
    def _closed_intervals(index):
        closed = ((end, value, key) for value, postings in index.items()
                  for key, intervals in postings.items() for _, end in intervals
                  if end is not None)
        return deque(sorted(closed, key=itemgetter(0)))

    def prune(self):
        """Apply retention now, including after an idle period; return floor."""
        with self._lock:
            self._check_transaction_configuration()
            return self._prune_policy(max(self._times[-1], datetime.now(timezone.utc)))

    def _prune_policy(self, now):
        floor = self._oldest_revision
        if self._max_revisions is not None:
            floor = max(floor, self._revision - self._max_revisions + 1)
        if self._max_age is not None:
            # Retain the state at the age boundary, including an older anchor
            # if no mutation occurred exactly at that time.
            try:
                cutoff = now - self._max_age
            except OverflowError:
                cutoff = datetime.min.replace(tzinfo=timezone.utc)
            floor = max(floor, self._oldest_revision + bisect_right(self._times, cutoff) - 1)
        if floor > self._oldest_revision:
            self._prune_before(floor)
        return self._oldest_revision

    @staticmethod
    def _trim_postings(index, floor):
        """Discard expired intervals using binary search over their starts."""
        result = {}
        for value, postings in index.items():
            retained = {}
            for key, intervals in postings.items():
                pos = bisect_right(intervals, floor, key=lambda interval: interval[0])
                if pos and (intervals[pos - 1][1] is None or intervals[pos - 1][1] > floor):
                    pos -= 1
                if pos < len(intervals):
                    active = intervals[pos:]
                    start, end = active[0]
                    if start < floor:
                        active[0] = [floor, end]
                    retained[key] = active
            if retained:
                result[value] = retained
        return result

    def compact(self):
        """Apply retention and repack history without creating a revision.

        Retained states and timestamps are unchanged. Unreachable objects are
        released to Python's memory manager; no global GC is forced. Return
        the retained floor and record counts before and after compaction.
        """
        with self._lock:
            self._check_transaction_configuration()
            before = sum(map(len, self._history.values()))
            self.prune()
            history = {}
            for key, records in self._history.items():
                packed = []
                old = _MISSING
                for revision, value in self._iter_history(records):
                    shareable = (old is not _MISSING and value is not _MISSING
                                 and _plain_tree(old) and _plain_tree(value))
                    packed.append(self._history_record(packed, revision, old,
                                                       value, shareable))
                    old = value
                if packed:
                    history[key] = packed
            floor = self._oldest_revision
            indexes = {field: self._trim_postings(index, floor)
                       for field, index in self._indexes.items()}
            composites = {fields: self._trim_postings(index, floor)
                          for fields, index in self._composite_indexes.items()}
            ranges = {field: {domain: list(values) for domain, values in groups.items()
                              if values}
                      for field, groups in self._range_indexes.items()}
            self._history, self._indexes = history, indexes
            self._composite_indexes, self._range_indexes = composites, ranges
            self._times = list(self._times)
            return {'oldest_revision': floor, 'records_before': before,
                    'records_after': sum(map(len, history.values()))}

    def _prune_before(self, floor):
        """Release expired entries while retaining the value visible at the boundary."""
        events = self._retention_events
        due = {}
        while events.history and events.history[0][0] <= floor:
            _, key = events.history.popleft()
            due[key] = None
        for key in due:
            records = self._history[key]
            due[key] = records  # Keep expired values alive until the new floor is published.
            pos = bisect_right(records, floor, key=lambda record: record[0])
            boundary_record = records[pos - 1]
            boundary = (self._at(key, floor) if boundary_record[2]
                        else boundary_record[1])
            retained = records[pos:]
            if boundary is not _MISSING:
                retained.insert(0, (floor, boundary, False))
            if retained:
                self._history[key] = retained
            else:
                del self._history[key]

        source = self._transaction_source
        for field, pending in events.indexes.items():
            index = self._indexes[field]
            base = None if source is None else source._indexes[field]
            removed = self._prune_index(index, pending, floor, base=base)
            if removed:
                base = None if source is None else source._range_indexes[field]
                self._prune_range_values(self._range_indexes[field], index, removed, base=base)
        for fields, pending in events.composites.items():
            base = None if source is None else source._composite_indexes[fields]
            self._prune_index(self._composite_indexes[fields], pending, floor, base=base)
        self._times = self._times[floor - self._oldest_revision:]
        self._oldest_revision = floor

    @staticmethod
    def _prune_index(index, pending, floor, *, base):
        removed = []
        while pending and pending[0][0] <= floor:
            _, value, key = pending.popleft()
            postings = index.get(value)
            if postings is None or key not in postings:
                continue
            intervals = postings[key]
            pos = bisect_right(intervals, floor, key=lambda interval: interval[0])
            if pos and (intervals[pos - 1][1] is None or intervals[pos - 1][1] > floor):
                pos -= 1
            if not pos:
                continue
            if base is not None and postings is base.get(value):
                postings = index[value] = postings.copy()
            if pos < len(intervals):
                postings[key] = intervals[pos:]
            else:
                del postings[key]
                if not postings:
                    del index[value]
                    removed.append(value)
        return removed

    @staticmethod
    def _prune_range_values(groups, index, removed, *, base):
        for value in removed:
            # Equality buckets may have been opened by a non-range numeric value.
            if type(value) is bool:
                value = int(value)
            elif type(value) is complex and value.imag == 0:
                value = value.real
            domain = _ordered_value(value)
            if domain is None:
                if type(value) not in _SHAREABLE_SCALARS:
                    # Custom hashable values may alias a sortable bucket.
                    for kind, values in tuple(groups.items()):
                        kept = [item for item in values if item in index]
                        if kept:
                            groups[kind] = kept
                        else:
                            del groups[kind]
                    return
                continue
            values = groups.get(domain, ())
            pos = bisect_left(values, value)
            if pos < len(values) and values[pos] == value:
                if base is not None and values is base.get(domain):
                    values = groups[domain] = values.copy()
                del values[pos]
                if not values:
                    del groups[domain]

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
                    return cls(self, key, (), self._key_revisions.get(key, 0))
            return deepcopy(value)

    @staticmethod
    def _field(value, field):
        if (type(value) is dict or isinstance(value, Mapping)) and field in value:
            result = value[field]
            hash(result)  # indexed attributes must be hashable
            return result
        return _MISSING

    @staticmethod
    def _history_record(records, revision, old, new, shareable):
        """Encode sparse top-level replacements only when smaller than a snapshot.

        Records are (revision, payload, is_delta). Full checkpoints cover all
        unsupported shapes, deletes, key-order changes and periodic boundaries.
        Identity comparisons preserve exact scalar values and key identities.
        Payloads contain only internally owned, structurally shared branches.
        """
        if (shareable and type(old) is dict and type(new) is dict
                and bool(records)
                and not all(record[2] for record in records[-(_CHECKPOINT_INTERVAL - 1):])
                and len(old) == len(new)
                and all(a is b for a, b in zip(old, new))):
            changes = tuple((key, value) for key, value in new.items()
                            if old[key] is not value)
            if getsizeof(changes) + sum(map(getsizeof, changes)) < getsizeof(new):
                return revision, changes, True
        return revision, new, False

    @staticmethod
    def _apply_delta(value, changes):
        """Reconstruct an owned root without modifying the preceding revision."""
        result = value.copy()
        result.update(changes)
        return result

    def _iter_history(self, records):
        """Decode a key's history in one forward pass for index rebuilding."""
        value = _MISSING
        floor = self._oldest_revision
        for revision, payload, is_delta in records:
            value = self._apply_delta(value, payload) if is_delta else payload
            yield floor if revision < floor else revision, value

    def _records(self, key):
        records = self._history.get(key)
        if records is not None:
            return records
        if self._stage is not None:
            value = self._data.get(key, _MISSING)
            if value is not _MISSING:
                return [(0, value, False)]
        return ()

    def _history_keys(self):
        if self._stage is None:
            return self._history
        added = (key for key, records in self._history.items() if records[0][0] != 0)
        return chain(self._stage.base_keys, added)

    def _commit(self, key, value, deleted=False, *, _owned=False, _batch=None):
        """Commit a revision; _owned is reserved for internal plain-tree updates."""
        hash(key)
        with self._lock:
            if self._atomic_callback:
                raise RuntimeError("Writes from an atomic update callback are not supported")
            if self._transaction_active:
                raise RuntimeError("Write through the transaction mapping")
            if deleted and key not in self._data:
                raise KeyError(key)
            old = self._data.get(key, _MISSING)
            flat = not deleted and _flat_scalar_dict(value)
            if deleted:
                new = _MISSING
            elif _owned or type(value) in _SHAREABLE_SCALARS:
                new = value
            elif flat:
                new = value.copy()
            else:
                new = deepcopy(value)
            new_plain = deleted or flat or type(new) in _SHAREABLE_SCALARS or _plain_tree(new)
            if flat and _flat_scalar_dict(old):
                # Immutable leaves already share by identity. Reuse the entire
                # record only when its keys, values and insertion order match.
                shareable = True
                if len(old) == len(new) and all(
                        old_key is new_key and old_value is new_value
                        for (old_key, old_value), (new_key, new_value) in zip(old.items(), new.items())):
                    new = old
            else:
                shareable = (not deleted and old is not _MISSING
                             and key not in self._unshareable_keys and new_plain)
                if shareable:
                    new = _share_tree(old, new)
            stage = self._stage
            if stage is not None and stage.indexes_pending and not new_plain:
                self._build_stage_indexes()
            # Validate and prepare before changing state.
            transitions = [(field, self._field(old, field), self._field(new, field))
                           for field in self._indexes]
            composite_transitions = [(fields, self._composite_value(old, fields),
                                      self._composite_value(new, fields))
                                     for fields in self._composite_indexes]
            revision = self._revision + 1 if _batch is None else _batch[0]
            # Keep the revision timeline sorted even if the system clock moves backward.
            timestamp = (max(self._times[-1], datetime.now(timezone.utc))
                         if _batch is None else _batch[1])
            records = self._history.get(key)
            if records is None:
                records = [(0, old, False)] if stage is not None and old is not _MISSING else []
            record = self._history_record(records, revision, old, new, shareable)
            source = self._transaction_source
            expiry = self._retention_events
            if stage is None or not stage.indexes_pending:
                for field, before, after in transitions:
                    if after is not _MISSING:
                        self._add_range_value(self._range_indexes[field], after,
                                              base=None if source is None else source._range_indexes[field])
                    if before == after:
                        continue
                    index = self._indexes[field]
                    if source is not None:
                        self._copy_posting_paths(index, source._indexes[field], key, before, after)
                    if before is not _MISSING:
                        index[before][key][-1][1] = revision
                        if expiry is not None:
                            expiry.indexes[field].append((revision, before, key))
                    if after is not _MISSING:
                        index.setdefault(after, {}).setdefault(key, []).append([revision, None])
                for fields, before, after in composite_transitions:
                    self._transition(self._composite_indexes[fields], key, revision, before, after,
                                     base=None if source is None else source._composite_indexes[fields],
                                     expiry=None if expiry is None else expiry.composites[fields])
            if expiry is not None and records:
                expiry.history.append((revision, key))
            self._history.setdefault(key, records).append(record)
            if new_plain:
                self._unshareable_keys.discard(key)
            else:
                self._unshareable_keys.add(key)
            if deleted:
                del self._data[key]
                self._key_revisions.pop(key, None)
                if stage is not None:
                    stage.inserted.pop(key, None)
            else:
                self._data[key] = new
                self._key_revisions[key] = revision
                if stage is not None and old is _MISSING:
                    stage.inserted[key] = None
            self._revision = revision
            if _batch is None:
                self._times.append(timestamp)
                self._prune_policy(timestamp)

    def _check_transaction_configuration(self):
        """Keep index definitions and retention fixed during a transaction."""
        if self._atomic_callback or self._transaction_active or self._optimistic_threads or self._stage is not None:
            raise RuntimeError("Configuration changes are not transactional")

    @staticmethod
    def _copy_posting_paths(index, base, key, before, after):
        """Detach the buckets and interval lists a commit is about to edit."""
        for value in (before, after):
            if value is _MISSING:
                continue
            original = base.get(value)
            if original is None:
                continue
            bucket = index[value]
            if bucket is original:
                bucket = index[value] = original.copy()
            intervals = original.get(key)
            if intervals is not None and bucket[key] is intervals:
                bucket[key] = intervals.copy()
                bucket[key][-1] = intervals[-1].copy()

    def _transaction_candidate(self, touched):
        """Copy the top-level tables and detach histories for touched keys."""
        candidate = ChronoDict(track_mutations=self._track_mutations,
                               max_revisions=self._max_revisions,
                               max_age=self._max_age)
        candidate._transaction_source = self
        candidate._data = self._data.copy()
        candidate._history = self._history.copy()
        for key in touched:
            if key in self._history:
                candidate._history[key] = self._history[key].copy()
        candidate._key_revisions = self._key_revisions.copy()
        candidate._unshareable_keys = self._unshareable_keys.copy()
        candidate._revision = self._revision
        candidate._oldest_revision = self._oldest_revision
        candidate._times = self._times.copy()
        candidate._indexes = {field: index.copy() for field, index in self._indexes.items()}
        candidate._composite_indexes = {fields: index.copy()
                                        for fields, index in self._composite_indexes.items()}
        candidate._range_indexes = {field: groups.copy() for field, groups in self._range_indexes.items()}
        if self._retention_events is not None:
            candidate._retention_events = self._retention_events.copy()
        return candidate

    def _new_transaction_stage(self):
        stage = ChronoDict(indexes=self._indexes, track_mutations=self._track_mutations,
                           backend=self.backend)
        for field in self._indexes:
            if not isinstance(field, str) or not field:
                raise ValueError('Index field must be a non-empty string')
        stage._data = self._data.copy()
        unshareable = self._unshareable_keys.copy()
        if unshareable:
            # Preserve deepcopy behavior for custom objects, cycles and aliases.
            for key, value in stage._data.items():
                if key in unshareable:
                    detached = stage._data[key] = deepcopy(value)
                    if not _plain_tree(detached):
                        stage._unshareable_keys.add(key)
        stage._composite_indexes = dict.fromkeys(self._composite_indexes)
        stage._stage = _TransactionStage(tuple(stage._data), bool(self._indexes or self._composite_indexes))
        if unshareable:
            stage._build_stage_indexes()
        return stage

    @contextmanager
    def transaction(self, *, optimistic=False):
        """Stage writes privately while other threads read committed state.

        Only one writer transaction may be active. Owner writes, competing
        transactions and configuration changes fail fast until it exits.
        The stage has a private revision timeline and remains detached on exit.
        Stages of plain values build their secondary indexes on first search.
        Publication and retention run under the owner lock.
        Optimistic mode permits concurrent writers but rejects a commit if
        any owner revision changed since staging began. Retry in a fresh context.
        """
        if type(optimistic) is not bool:
            raise TypeError("optimistic must be a bool")
        thread = get_ident()
        with self._lock:
            if (self._atomic_callback or self._transaction_active or self._stage is not None
                    or thread in self._optimistic_threads
                    or (not optimistic and self._optimistic_threads)):
                raise RuntimeError("Nested or incompatible concurrent transactions are not supported")
            if optimistic:
                self._optimistic_threads.add(thread)
            else:
                self._transaction_active = True
        try:
            with self._lock:
                base_revision = self._revision
                stage = self._new_transaction_stage()
            yield stage
            with self._lock, stage._lock:
                if optimistic and self._revision != base_revision:
                    raise RuntimeError("Transaction conflict: committed revision changed; retry")
                # The copy-on-write candidate needs a stable owner even if a
                # Python callback attempts a reentrant write during publication.
                self._transaction_active = True
                stage._transaction_active = True
                try:
                    self._publish_transaction(stage)
                finally:
                    stage._transaction_active = False
                    if optimistic:
                        self._transaction_active = False
        finally:
            with self._lock:
                if optimistic:
                    self._optimistic_threads.remove(thread)
                else:
                    self._transaction_active = False

    def _publish_transaction(self, stage):
        touched = [key for key in stage._history if key in stage._data or key in self._data]
        if not touched:
            return
        candidate = self._transaction_candidate(touched)
        revision = self._revision + 1
        timestamp = max(self._times[-1], datetime.now(timezone.utc))
        for key in touched:
            deleted = key not in stage._data
            candidate._commit(key, stage._data.get(key), deleted=deleted,
                              _batch=(revision, timestamp))
        # Move surviving inserts and reinserts to their final stage order.
        for key in stage._stage.inserted:
            candidate._data[key] = candidate._data.pop(key)
        candidate._times.append(timestamp)
        candidate._prune_policy(timestamp)
        # Publish only after every validation and retention step succeeds.
        for attr in ('_data', '_history', '_key_revisions', '_unshareable_keys',
                     '_revision', '_oldest_revision', '_times', '_indexes',
                     '_composite_indexes', '_range_indexes', '_retention_events'):
            setattr(self, attr, getattr(candidate, attr))

    def __setitem__(self, key, value):
        if type(value) is _TrackedList:
            with self._lock:
                if value._consume_inplace_assignment(self, key, ()):
                    return
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
        if revision < self._oldest_revision:
            raise ValueError('Revision is no longer retained')
        return revision

    def revision_at(self, timestamp):
        """Return the latest revision at or before an aware datetime.

        Before construction, the dictionary is treated as empty (revision 0).
        Equal commit timestamps resolve to the latest matching revision.
        """
        if not isinstance(timestamp, datetime):
            raise TypeError('timestamp must be a datetime')
        if timestamp.utcoffset() is None:
            raise ValueError('timestamp must be timezone-aware')
        timestamp = timestamp.astimezone(timezone.utc)
        with self._lock:
            if self._oldest_revision and timestamp < self._times[0]:
                raise ValueError('Timestamp is no longer retained')
            return self._oldest_revision + max(0, bisect_right(self._times, timestamp) - 1)

    def timestamp(self, version=None):
        """Return the UTC timestamp of a revision (default: latest)."""
        with self._lock:
            return self._times[self._validate_revision(version) - self._oldest_revision]

    def _resolve_revision(self, version, at):
        """Resolve a revision number or timestamp, never both."""
        if at is not None:
            if version is not None:
                raise ValueError('Specify either version or at, not both')
            return self.revision_at(at)
        return self._validate_revision(version)

    def _at(self, key, revision):
        records = self._history.get(key)
        if records is None:
            return self._data.get(key, _MISSING) if self._stage is not None else _MISSING
        pos = bisect_right(records, revision, key=lambda record: record[0]) - 1
        if pos < 0:
            return _MISSING
        if pos == len(records) - 1:
            return self._data.get(key, _MISSING)
        start = pos
        while records[start][2]:
            start -= 1
        value = records[start][1]
        for i in range(start + 1, pos + 1):
            value = self._apply_delta(value, records[i][1])
        return value

    def get(self, key, default=None, *, version=None, at=None):
        with self._lock:
            if version is None and at is None:
                value = self._data.get(key, _MISSING)
            else:
                value = self._at(key, self._resolve_revision(version, at))
            return default if value is _MISSING else _copy_value(value)

    def patch(self, key, **changes):
        """Apply top-level field changes as one revision."""
        with self._lock:
            current = self._data[key]
            if not isinstance(current, dict):
                raise TypeError('patch requires a dict value')
            # Plain trees can safely share untouched branches. Copy only the
            # incoming fields, then transfer ownership of the new root.
            if type(self) is ChronoDict and type(current) is dict and _plain_tree(current) and _plain_tree(changes):
                value = current.copy()
                value.update(_copy_value(changes))
                self._commit(key, value, _owned=True)
            else:
                # Preserve deepcopy and subclass dispatch for arbitrary objects.
                value = deepcopy(current)
                value.update(changes)
                self[key] = value

    def atomic_update(self, key, transform):
        """Replace an existing value using a serialized read-modify-write.

        The callback receives a detached copy and returns the replacement.
        It may read this mapping but must not mutate it or wait for another
        thread accessing it. Callback exceptions leave the mapping unchanged.
        """
        if not callable(transform):
            raise TypeError('transform must be callable')
        with self._lock:
            if self._transaction_active or self._atomic_callback:
                raise RuntimeError('Atomic update is not available during this operation')
            self._atomic_callback = True
            try:
                replacement = transform(deepcopy(self._data[key]))
                # Prepare the detached return value before committing.
                result = deepcopy(replacement)
            finally:
                self._atomic_callback = False
            self[key] = replacement
            return result

    def setdefault(self, key, default=None):
        """Insert a missing key exactly once, even with competing threads."""
        with self._lock:
            if key not in self._data:
                self[key] = default
            return self[key]

    def history(self, key):
        with self._lock:
            return [{'version': r, 'timestamp': self._times[r - self._oldest_revision], 'deleted': v is _MISSING,
                     'value': None if v is _MISSING else deepcopy(v)}
                    for r, v in self._iter_history(self._records(key))]

    def snapshot(self, version=None, *, at=None):
        """Capture one committed revision, then copy safe trees outside the lock.

        Captured roots remain alive even if retention prunes their revisions.
        Custom objects and aliased or cyclic containers retain locked copying.
        """
        with self._lock:
            revision = self._resolve_revision(version, at)
            captured = []
            for key in self._history_keys():
                value = self._at(key, revision)
                if value is _MISSING:
                    continue
                deferred = _plain_tree(value)
                captured.append((key, value if deferred else deepcopy(value), deferred))
        return {key: deepcopy(value) if deferred else value
                for key, value, deferred in captured}

    @staticmethod
    def _add_range_value(groups, value, *, base=None):
        """Maintain sorted distinct historical values per comparable domain."""
        domain = _ordered_value(value)
        if domain is None:
            return
        values = groups.setdefault(domain, [])
        pos = bisect_left(values, value)
        if pos == len(values) or values[pos] != value:
            if base is not None and values is base.get(domain):
                values = groups[domain] = values.copy()
            values.insert(pos, value)

    def _build_stage_indexes(self):
        """Index a stage's private timeline when an indexed query needs it."""
        if not self._stage.indexes_pending:
            return
        indexes = {}
        ranges = {}
        for field in self._indexes:
            indexes[field], ranges[field] = self._build_index(field)
        composites = {fields: self._build_composite_index(fields)
                      for fields in self._composite_indexes}
        self._indexes, self._range_indexes, self._composite_indexes = indexes, ranges, composites
        self._stage.indexes_pending = False

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

    def _active_keys(self, postings, revision):
        if self._native_posting_keys is not None and self._revision <= _MAX_NATIVE_REVISION:
            return self._native_posting_keys(postings, revision)
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
        if (type(value) is not dict and not isinstance(value, Mapping)) or field not in value:
            return False
        actual = value[field]
        if op is None:
            # Do not expose owned mutable branches to a query object's __eq__.
            return _copy_value(actual) == target
        return (_ordered_value(actual) == _ordered_value(target)
                and _RANGE_OPS[op](actual, target))

    def find(self, *, version=None, at=None, **conditions):
        """Search current or historical values using equality and scalar ranges.

        Range suffixes: __gt, __gte, __lt, __lte. Incompatible scalar
        domains and missing fields do not match. Unindexed fields are scanned.
        An explicitly indexed literal field name takes precedence over a suffix.
        """
        with self._lock:
            revision = self._resolve_revision(version, at)
            return self._find(revision, conditions)

    def find_changed(self, from_version, to_version, /, **conditions):
        """Return matching end-state values with a write in (from_version, to_version].

        Repeated assignments and writes later reverted within the interval count.
        Keys deleted at the end are absent from the results.
        """
        with self._lock:
            if from_version is None or to_version is None:
                raise ValueError('Specify both revisions')
            start = self._validate_revision(from_version)
            end = self._validate_revision(to_version)
            if start > end:
                raise ValueError('from_version must precede or equal to_version')
            return self._find(end, conditions, changed_after=start)

    def _find(self, revision, conditions, *, changed_after=None):
        if conditions and self._stage is not None:
            self._build_stage_indexes()
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
        if candidates is not None:
            keys = candidates
        elif changed_after is not None:
            keys = self._history
        else:
            keys = self._history_keys()
        result = {}
        for key in keys:
            if changed_after is not None:
                records = self._history.get(key, ())
                pos = bisect_right(records, changed_after, key=itemgetter(0))
                if pos == len(records) or records[pos][0] > revision:
                    continue
            value = self._at(key, revision)
            if value is not _MISSING and all(
                    self._matches(value, field, op, target)
                    for field, op, target in predicates):
                result[key] = _copy_value(value)
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
            self._check_transaction_configuration()
            if field in self._indexes:
                raise ValueError(f'Index already exists: {field}')
            index, ranges = self._build_index(field)
            if self._retention_events is not None:
                self._retention_events.indexes[field] = self._closed_intervals(index)
            self._indexes[field] = index
            self._range_indexes[field] = ranges

    def _build_index(self, field):
        index = {}
        ranges = {}
        for key in self._history_keys():
            before = _MISSING
            for revision, value in self._iter_history(self._records(key)):
                after = self._field(value, field)
                if after is not _MISSING:
                    self._add_range_value(ranges, after)
                self._transition(index, key, revision, before, after)
                before = after
        return index, ranges

    def drop_index(self, field):
        """Remove an index without changing values or revision history."""
        with self._lock:
            self._check_transaction_configuration()
            del self._indexes[field]
            del self._range_indexes[field]
            if self._retention_events is not None:
                del self._retention_events.indexes[field]

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
    def _transition(index, key, revision, before, after, *, base=None, expiry=None):
        """Close and open temporal postings when an indexed value changes."""
        if before is after or (before is not _MISSING and after is not _MISSING
                               and before == after):
            return
        if base is not None:
            ChronoDict._copy_posting_paths(index, base, key, before, after)
        if before is not _MISSING:
            index[before][key][-1][1] = revision
            if expiry is not None:
                expiry.append((revision, before, key))
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
            self._check_transaction_configuration()
            if fields in self._composite_indexes:
                raise ValueError(f'Composite index already exists: {fields}')
            index = self._build_composite_index(fields)
            if self._retention_events is not None:
                self._retention_events.composites[fields] = self._closed_intervals(index)
            self._composite_indexes[fields] = index

    def _build_composite_index(self, fields):
        index = {}
        for key in self._history_keys():
            before = _MISSING
            for revision, value in self._iter_history(self._records(key)):
                after = self._composite_value(value, fields)
                self._transition(index, key, revision, before, after)
                before = after
        return index

    def drop_composite_index(self, *fields):
        """Remove a composite index without modifying data or history."""
        fields = self._normalize_composite(fields)
        with self._lock:
            self._check_transaction_configuration()
            del self._composite_indexes[fields]
            if self._retention_events is not None:
                del self._retention_events.composites[fields]

    def branch(self, name, *, version=None, at=None):
        """Fork an independent mapping through the selected retained revision."""
        if not isinstance(name, str) or not name.strip() or name == 'main':
            raise ValueError("A branch requires a non-empty name other than 'main'")
        with self._lock:
            self._check_transaction_configuration()
            if name in self._branches:
                raise ValueError(f'Branch already exists: {name}')
            revision = self._resolve_revision(version, at)
            self._atomic_callback = True
            try:
                fork = self._fork_at(revision)
                self._branches[name] = _Branch(fork)
            finally:
                self._atomic_callback = False
            return fork

    def _fork_at(self, revision):
        fork = ChronoDict(track_mutations=self._track_mutations, backend=self.backend,
                          max_revisions=self._max_revisions, max_age=self._max_age)
        fork._revision = revision
        fork._oldest_revision = self._oldest_revision
        fork._times = self._times[:revision - self._oldest_revision + 1]
        for key, history in self._history.items():
            stop = bisect_right(history, revision, key=itemgetter(0))
            if not stop:
                continue
            records = history[:stop]
            for index, (stamp, payload, is_delta) in enumerate(records):
                if not is_delta and payload is not _MISSING and not _plain_tree(payload):
                    records[index] = stamp, deepcopy(payload), False
            fork._history[key] = records
            stamp, value, is_delta = records[-1]
            if value is _MISSING:
                continue
            if is_delta:
                value = self._at(key, revision)
            fork._data[key] = value
            current_stamp = self._key_revisions.get(key, self._revision + 1)
            fork._key_revisions[key] = current_stamp if current_stamp <= revision else stamp
            if not _plain_tree(value):
                fork._unshareable_keys.add(key)
        if revision == self._revision:
            fork._data = {key: fork._data[key] for key in self._data}
        for field in self._indexes:
            fork._indexes[field], fork._range_indexes[field] = fork._build_index(field)
        fork._composite_indexes = {fields: fork._build_composite_index(fields)
                                  for fields in self._composite_indexes}
        if fork._retention_events is not None:
            fork._retention_events = fork._schedule_retention()
        return fork

    def compare(self, left, right):
        """Return per-key differences between named branch heads or 'main'."""
        with self._lock:
            left_branch = None if left == 'main' else self._branches[left]
            right_branch = None if right == 'main' else self._branches[right]
        left_mapping = self if left_branch is None else left_branch.mapping
        right_mapping = self if right_branch is None else right_branch.mapping
        if left_mapping is right_mapping:
            return {}
        first, second = sorted((left_mapping, right_mapping), key=id)
        with first._lock, second._lock:
            result = {}
            for key in dict.fromkeys(chain(left_mapping._data, right_mapping._data)):
                left_stamp = (self._key_revisions.get(key, _MISSING) if left_branch is None
                              else left_branch.parent_revision(key))
                right_stamp = (self._key_revisions.get(key, _MISSING) if right_branch is None
                               else right_branch.parent_revision(key))
                if left_stamp is not _UNCHANGED and left_stamp == right_stamp:
                    continue
                before = left_mapping._data.get(key, _MISSING)
                after = right_mapping._data.get(key, _MISSING)
                if not _same_branch_value(before, after):
                    result[key] = _describe_change(before, after)
            return result

    def merge(self, name):
        """Merge a child branch into this mapping and return the resulting revision.

        Competing writes to a key conflict unless both heads agree. Successful
        merges update the shared checkpoints for the branch keys they accept.
        """
        with self._lock:
            branch = self._branches[name]
        fork = branch.mapping
        first, second = sorted((self, fork), key=id)
        with first._lock, second._lock:
            self._check_transaction_configuration()
            fork._check_transaction_configuration()
            fork._atomic_callback = True
            try:
                self._atomic_callback = True
                try:
                    writes, checkpoints = self._plan_merge(branch)
                finally:
                    self._atomic_callback = False
                if writes:
                    with self.transaction() as stage:
                        for key, value in writes.items():
                            if value is _MISSING:
                                del stage[key]
                            else:
                                stage[key] = value
                branch.checkpoints = checkpoints
                return self._revision
            finally:
                fork._atomic_callback = False

    def _plan_merge(self, branch):
        fork = branch.mapping
        checkpoints = branch.checkpoints.copy()
        writes = {}
        conflicts = []
        for key in dict.fromkeys(chain(fork._data, checkpoints)):
            parent_base, fork_base = checkpoints.get(key, (_MISSING, _MISSING))
            fork_stamp = fork._key_revisions.get(key, _MISSING)
            if fork_stamp == fork_base:
                continue
            parent_stamp = self._key_revisions.get(key, _MISSING)
            ours = self._data.get(key, _MISSING)
            theirs = fork._data.get(key, _MISSING)
            if parent_stamp != parent_base:
                if not _same_branch_value(ours, theirs):
                    conflicts.append(key)
                    continue
            else:
                writes[key] = theirs
                parent_stamp = _MISSING if theirs is _MISSING else self._revision + 1
            if parent_stamp is _MISSING and fork_stamp is _MISSING:
                checkpoints.pop(key, None)
            else:
                checkpoints[key] = parent_stamp, fork_stamp
        if conflicts:
            raise MergeConflictError(conflicts)
        return writes, checkpoints

    def rollback(self, key, *, version=None, at=None):
        """Restore a key at a revision, recording a new revision."""
        with self._lock:
            if version is None and at is None:
                raise ValueError('Specify version or at for rollback')
            value = self._at(key, self._resolve_revision(version, at))
            if value is _MISSING:
                if key in self._data:
                    del self[key]
            else:
                self[key] = value

    def diff(self, key, *, from_version=None, to_version=None, from_at=None, to_at=None):
        with self._lock:
            if (from_version is None and from_at is None) or (to_version is None and to_at is None):
                raise ValueError('Specify both comparison endpoints')
            before = self._at(key, self._resolve_revision(from_version, from_at))
            after = self._at(key, self._resolve_revision(to_version, to_at))
            return _describe_change(before, after)
