"""Phase 3 storage regression tests and reproducible memory benchmark.

Run as a unittest module for deterministic checks, or directly for the
benchmark. Internal assertions intentionally cover storage implementation.
"""
import gc
import random
import sys
import tracemalloc
import unittest
import weakref
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from chronodict import ChronoDict


class Clock(datetime):
    """Controllable UTC clock for deterministic retention checks."""
    current = datetime(2030, 1, 1, tzinfo=timezone.utc)

    @classmethod
    def now(cls, tz=None):
        return cls.current if tz is None else cls.current.astimezone(tz)


class Payload:
    pass


def workload():
    """Identical sparse-update workload for tests and benchmarks."""
    d = ChronoDict(indexes=['f0'])
    d[1] = {f'f{i}': i for i in range(100)}
    for i in range(1, 101):
        d.patch(1, f0=i)
    return d


def root_bytes(d):
    """Shallow unique root/delta payload bytes, excluding nested values."""
    seen = set()
    total = 0
    def add(obj):
        nonlocal total
        if id(obj) not in seen:
            seen.add(id(obj))
            total += sys.getsizeof(obj)
    for records in d._history.values():
        for _, payload, delta in records:
            add(payload)
            if delta:
                for pair in payload:
                    add(pair)
    for value in d._data.values():
        add(value)
    return total


class PhaseThreeStorageTests(unittest.TestCase):
    def test_structural_sharing_and_isolation(self):
        d = ChronoDict()
        d[1] = {'score': 1, 'nested': {'items': [1]}}
        old = d._data[1]
        d.patch(1, score=2)
        self.assertIs(old['nested'], d._data[1]['nested'])
        d[1]['nested']['items'].append(2)
        self.assertEqual(d.get(1, version=2)['nested']['items'], [1])
        self.assertEqual(d.get(1)['nested']['items'], [1, 2])

    def test_copy_efficient_patch(self):
        d = ChronoDict()
        d[1] = {'nested': [1], 'score': 1}
        incoming = [2]
        with patch('chronodict.core.deepcopy', wraps=deepcopy) as copy:
            d.patch(1, extra=incoming)
            self.assertEqual(copy.call_count, 1)
            self.assertEqual(copy.call_args.args[0], {'extra': [2]})
        incoming.append(3)
        self.assertEqual(d.get(1)['extra'], [2])

    def test_cycles_and_aliases(self):
        d = ChronoDict()
        shared = []
        root = {'a': shared, 'b': shared}
        root['cycle'] = root
        d[1] = root
        d.patch(1, score=2)
        for version in (1, 2):
            value = d.get(1, version=version)
            self.assertIs(value['a'], value['b'])
            self.assertIs(value['cycle'], value)
        shared.append(3)
        self.assertEqual(d.get(1)['a'], [])

    def test_delta_checkpoints_and_memory(self):
        d = workload()
        records = d._history[1]
        self.assertTrue(any(record[2] for record in records))
        run = 0
        for version, _, delta in records:
            run = run + 1 if delta else 0
            self.assertLessEqual(run, 31)
            self.assertEqual(d.get(1, version=version)['f0'], version - 1)
        baseline = sum(sys.getsizeof(d.get(1, version=v)) for v in range(1, 102))
        self.assertLess(root_bytes(d), baseline)
        d.create_index('f1')
        self.assertEqual(d.find(f0=20, f1=1, version=21), {1: d.get(1, version=21)})

    def test_count_retention_and_expired_queries(self):
        d = ChronoDict(indexes=['score'], max_revisions=3)
        for i in range(10):
            d[1] = {'score': i}
        self.assertEqual(d.oldest_revision, 8)
        self.assertEqual(d.snapshot(8), {1: {'score': 7}})
        for operation in (lambda: d.snapshot(7), lambda: d.find(version=7, score=6), lambda: d.rollback(1, version=7)):
            with self.assertRaises(ValueError):
                operation()
        d.set_retention(max_revisions=None)
        self.assertEqual(d.oldest_revision, 8)

    def test_time_retention_and_clock_reversal(self):
        Clock.current = datetime(2030, 1, 1, tzinfo=timezone.utc)
        with patch('chronodict.core.datetime', Clock):
            d = ChronoDict(max_age=timedelta(seconds=5))
            d[1] = 1
            Clock.current += timedelta(seconds=4)
            d[1] = 2
            Clock.current += timedelta(seconds=4)
            d[1] = 3
            self.assertEqual(d.oldest_revision, 1)
            old = d.timestamp()
            Clock.current -= timedelta(seconds=2)
            d[1] = 4
            self.assertEqual(d.timestamp(), old)
            Clock.current += timedelta(seconds=20)
            d.prune()
            self.assertEqual(d.oldest_revision, 4)
            self.assertEqual(d[1], 4)

    def test_invalid_retention(self):
        for options in ({'max_revisions': 0}, {'max_revisions': True}, {'max_age': timedelta(0)}, {'max_age': 10}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                ChronoDict(**options)
        d = ChronoDict(max_revisions=2)
        with self.assertRaises(ValueError):
            d.set_retention(max_revisions=-1)
        for i in range(5):
            d[1] = i
        self.assertEqual(d.oldest_revision, 4)

    def test_compaction_randomized_retained_states(self):
        rng = random.Random(17)
        d = ChronoDict(indexes=['score'], max_revisions=15)
        d.create_composite_index('team', 'score')
        state, snapshots = {}, {0: {}}
        for revision in range(1, 101):
            key = rng.randrange(6)
            if key in state and rng.randrange(4) == 0:
                del state[key]
                del d[key]
            else:
                value = {'score': rng.randrange(5), 'team': rng.choice(['A', 'B'])}
                state[key] = value
                d[key] = value
            snapshots[revision] = deepcopy(state)
            if revision % 10 == 0:
                times = {v: d.timestamp(v) for v in range(d.oldest_revision, revision + 1)}
                d.compact()
                d.compact()
                self.assertEqual(d.revision, revision)
                for v, timestamp in times.items():
                    self.assertEqual(d.timestamp(v), timestamp)
                    self.assertEqual(d.snapshot(v), snapshots[v])
                    for team in ('A', 'B'):
                        for score in range(5):
                            expected = {k: val for k, val in snapshots[v].items() if val['team'] == team and val['score'] == score}
                            self.assertEqual(d.find(version=v, team=team, score=score), expected)
                            expected = {k: val for k, val in snapshots[v].items() if val['team'] == team and val['score'] > score}
                            self.assertEqual(d.find(version=v, team=team, score__gt=score), expected)

    def test_compaction_releases_expired_objects(self):
        d = ChronoDict()
        d[1] = Payload()
        ref = weakref.ref(d._data[1])
        del d[1]
        d[2] = 2
        self.assertIsNotNone(ref())
        d.set_retention(max_revisions=1)
        d.compact()
        gc.collect()
        self.assertIsNone(ref())
        self.assertEqual(d.snapshot(), {2: 2})

    def test_live_view_survives_compaction(self):
        d = ChronoDict(max_revisions=2)
        d[1] = {'items': []}
        view = d[1]['items']
        for i in range(5):
            d[2] = i
        d.compact()
        view.append(1)
        self.assertEqual(d.get(1), {'items': [1]})


def benchmark():
    """Report shallow payload and traced allocation figures; no timing gates."""
    gc.collect()
    tracemalloc.start()
    try:
        d = workload()
        current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    baseline = sum(sys.getsizeof(d.get(1, version=v)) for v in range(1, 102))
    actual = root_bytes(d)
    print(f'Independent full-root reference: {baseline:,} bytes')
    print(f'ChronoDict unique root/delta payloads: {actual:,} bytes')
    print(f'Payload reduction: {100 * (1 - actual / baseline):.2f}%')
    print(f'Traced live / peak allocations: {current:,} / {peak:,} bytes')


if __name__ == '__main__':
    benchmark()
    unittest.main()
