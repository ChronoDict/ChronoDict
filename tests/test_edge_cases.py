"""Consolidated Phase 2/3 edge cases; deterministic, dependency-free tests.

Run alongside test_core.py and test_memory.py. Failures are not suppressed.
"""
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from math import copysign
from chronodict import ChronoDict


class Clock(datetime):
    current = datetime(2035, 1, 1, tzinfo=timezone.utc)

    @classmethod
    def now(cls, tz=None):
        return cls.current if tz is None else cls.current.astimezone(tz)


class IndexEdges(unittest.TestCase):
    def test_missing_and_none(self):
        d = ChronoDict(indexes=['x'])
        d[1] = {'x': None}
        d[2] = {}
        d[3] = None
        self.assertEqual(d.find(x=None), {1: {'x': None}})
        self.assertEqual(d.find(x=None, version=0), {})

    def test_index_lifecycle(self):
        d = ChronoDict({1: {'x': 1}})
        d.create_index('x')
        with self.assertRaises(ValueError):
            d.create_index('x')
        with self.assertRaises(ValueError):
            d.create_index('')
        with self.assertRaises(KeyError):
            d.drop_index('missing')
        d.drop_index('x')
        self.assertEqual(d.indexed_fields, ())
        self.assertEqual(d.revision, 1)

    def test_range_domains(self):
        d = ChronoDict(indexes=['x'])
        for k, x in enumerate([1, 2.5, 'a', 'z', b'a', b'z', None, float('nan')]):
            d[k] = {'x': x}
        for target, expected in [(1, {1}), ('a', {3}), (b'a', {5})]:
            with self.subTest(target=target):
                self.assertEqual(set(d.find(x__gt=target)), expected)
        for target in (None, [], float('nan')):
            with self.subTest(target=target), self.assertRaises(TypeError):
                d.find(x__gt=target)

    def test_composite_validation(self):
        d = ChronoDict()
        for fields in [('x',), ('x', 'x'), ('x', ''), ('x', 1)]:
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                d.create_composite_index(*fields)
        d.create_composite_index('y', 'x')
        self.assertEqual(d.composite_indexes, (('x', 'y'),))
        with self.assertRaises(ValueError):
            d.create_composite_index('x', 'y')
        d.drop_composite_index('y', 'x')
        self.assertEqual(d.composite_indexes, ())

    def test_failed_composite_build_and_write(self):
        d = ChronoDict({1: {'x': [], 'y': 1}})
        with self.assertRaises(TypeError):
            d.create_composite_index('x', 'y')
        self.assertEqual(d.composite_indexes, ())
        d = ChronoDict(indexes=['x'])
        d.create_composite_index('x', 'y')
        d[1] = {'x': 1, 'y': 2}
        with self.assertRaises(TypeError):
            d.patch(1, x=3, y=[])
        self.assertEqual(d.revision, 1)
        self.assertEqual(d.find(x=1, y=2), {1: {'x': 1, 'y': 2}})
        self.assertEqual(d.find(x=3), {})

    def test_repeated_delete_reinsert(self):
        d = ChronoDict(indexes=['x'])
        d.create_composite_index('x', 'y')
        for _ in range(3):
            d[1] = {'x': 1, 'y': 2}
            del d[1]
        for revision in range(7):
            expected = {1: {'x': 1, 'y': 2}} if revision % 2 else {}
            self.assertEqual(d.find(version=revision, x=1, y=2), expected)
            self.assertEqual(d.find(version=revision, x__gte=1), expected)


class MutationEdges(unittest.TestCase):
    def test_dict_mutators(self):
        d = ChronoDict({1: {'x': 1}}, indexes=['x'])
        view = d[1]
        view.update(x=2, y=3)
        self.assertEqual(d.revision, 2)
        self.assertEqual(view.pop('y'), 3)
        self.assertEqual(view.setdefault('x', 9), 2)
        self.assertEqual(d.revision, 3)
        view.setdefault('z', 4)
        self.assertEqual(view.popitem(), ('z', 4))
        view.clear()
        self.assertEqual(d.get(1), {})
        self.assertEqual(d.find(x=2), {})
        self.assertEqual(d.get(1, version=1), {'x': 1})

    def test_list_mutators(self):
        d = ChronoDict({1: [3, 1]})
        v = d[1]
        v.extend([2, 4]); v.insert(0, 5); v.sort(); v.reverse()
        self.assertEqual(d.get(1), [5, 4, 3, 2, 1])
        v[1:3] = [9]
        v.remove(2)
        self.assertEqual(v.pop(), 1)
        del v[0]
        self.assertEqual(d.get(1), [9])
        v.clear()
        self.assertEqual(d.get(1), [])
        self.assertEqual(d.get(1, version=1), [3, 1])

    def test_failed_mutation(self):
        d = ChronoDict({1: [1, 2]})
        with self.assertRaises(ValueError):
            d[1].remove(99)
        with self.assertRaises(IndexError):
            d[1][99] = 0
        self.assertEqual(d.revision, 1)
        self.assertEqual(d.get(1), [1, 2])

    def test_stale_after_delete_reinsert(self):
        d = ChronoDict({1: {'items': []}})
        v = d[1]['items']
        del d[1]
        d[1] = {'items': []}
        with self.assertRaises(RuntimeError):
            v.append(1)
        self.assertEqual(d.revision, 3)

    def test_disabled_tracking(self):
        d = ChronoDict({1: {'items': []}}, track_mutations=False)
        d[1]['items'].append(1)
        self.assertEqual(d.get(1), {'items': []})
        self.assertEqual(d.revision, 1)

    def test_detached_public_results(self):
        d = ChronoDict({1: {'x': 1, 'items': []}}, indexes=['x'])
        for result in (d.get(1), d.snapshot()[1], d.find(x=1)[1],
                       d.history(1)[0]['value'], d.diff(1, from_version=0, to_version=1)['after']):
            result['items'].append(5)
        self.assertEqual(d.get(1), {'x': 1, 'items': []})
        self.assertEqual(d.revision, 1)


class TimestampEdges(unittest.TestCase):
    def test_duplicate_timestamps_and_offsets(self):
        Clock.current = datetime(2035, 1, 1, tzinfo=timezone.utc)
        with patch('chronodict.core.datetime', Clock):
            d = ChronoDict()
            d[1] = 1
            d[1] = 2
        self.assertEqual(d.revision_at(Clock.current), 2)
        offset = timezone(timedelta(hours=5, minutes=30))
        self.assertEqual(d.get(1, at=Clock.current.astimezone(offset)), 2)
        self.assertEqual(d.revision_at(Clock.current - timedelta(seconds=1)), 0)

    def test_revision_validation(self):
        d = ChronoDict({1: 1})
        for revision in (-1, 2, True, 1.0, '1'):
            with self.subTest(revision=revision), self.assertRaises(ValueError):
                d.snapshot(revision)
        for op in (lambda: d.get(1, version=1, at=d.timestamp()),
                   lambda: d.find(version=1, at=d.timestamp()),
                   lambda: d.rollback(1), lambda: d.diff(1, from_version=1)):
            with self.assertRaises(ValueError):
                op()

    def test_diff_none_vs_absent(self):
        d = ChronoDict({1: None})
        del d[1]
        self.assertEqual(d.diff(1, from_version=1, to_version=2),
                         {'before_exists': True, 'after_exists': False, 'before': None, 'after': None})
        d.rollback(1, version=1)
        self.assertIn(1, d)
        self.assertIsNone(d[1])


class StorageEdges(unittest.TestCase):
    def test_scalar_type_and_signed_zero(self):
        d = ChronoDict({1: {'x': 0.0}})
        d.patch(1, x=-0.0)
        d.patch(1, x=0)
        self.assertEqual(copysign(1, d.get(1, version=1)['x']), 1)
        self.assertEqual(copysign(1, d.get(1, version=2)['x']), -1)
        self.assertIs(type(d.get(1)['x']), int)

    def test_delta_shape_changes_and_reinsertion(self):
        d = ChronoDict()
        base = {str(i): i for i in range(80)}
        d[1] = base
        d.patch(1, **{'0': 100})
        del d[1]['1']
        d[1]['new'] = 10
        del d[1]
        d[1] = dict(reversed(list(base.items())))
        expected = [d.snapshot(i) for i in range(7)]
        d.compact()
        for i, state in enumerate(expected):
            self.assertEqual(d.snapshot(i), state)
            if 1 in state:
                self.assertEqual(list(d.get(1, version=i)), list(state[1]))
        self.assertEqual(list(d.get(1)), list(reversed(base)))

    def test_combined_retention_and_expired_timestamp(self):
        Clock.current = datetime(2035, 1, 1, tzinfo=timezone.utc)
        with patch('chronodict.core.datetime', Clock):
            d = ChronoDict(max_revisions=3, max_age=timedelta(seconds=3))
            for i in range(6):
                Clock.current += timedelta(seconds=2)
                d[1] = i
            self.assertEqual(d.oldest_revision, 4)
            with self.assertRaises(ValueError):
                d.get(1, at=Clock.fromtimestamp((Clock.current - timedelta(seconds=5)).timestamp(), timezone.utc))
            Clock.current += timedelta(seconds=10)
            self.assertEqual(d.prune(), 6)
            d.set_retention(max_revisions=None, max_age=None)
            self.assertEqual(d.oldest_revision, 6)
            self.assertEqual(d.get(1), 5)

    def test_boundary_anchor_and_index_rebuild(self):
        d = ChronoDict(indexes=['x'], max_revisions=3)
        d[1] = {'x': 1, 'y': 2}
        for i in range(6):
            d[2] = {'x': i, 'y': 2}
        d.create_composite_index('x', 'y')
        d.drop_index('x'); d.create_index('x')
        for v in range(d.oldest_revision, d.revision + 1):
            self.assertEqual(d.get(1, version=v), {'x': 1, 'y': 2})
            self.assertIn(1, d.find(version=v, x=1, y=2))
            self.assertIn(1, d.find(version=v, x__gte=1))

    def test_empty_and_single_revision_compaction(self):
        d = ChronoDict(max_revisions=1)
        self.assertEqual(d.compact(), {'oldest_revision': 0, 'records_before': 0, 'records_after': 0})
        d[1] = 1
        del d[1]
        self.assertEqual(d.compact()['records_after'], 0)
        self.assertEqual(d.snapshot(), {})
        self.assertEqual(d.revision, 2)

    def test_historical_index_bool_numeric_transition(self):
        # Equality may share postings, but range queries have typed domains.
        d = ChronoDict(indexes=['x'])
        d[1] = {'x': True}
        d.patch(1, x=1)
        self.assertEqual(d.find(x__gte=1), {1: {'x': 1}})
        self.assertEqual(d.find(x__gte=1, version=1), {})
        d.compact()
        self.assertEqual(d.find(x__gte=1), {1: {'x': 1}})


if __name__ == '__main__':
    unittest.main()
