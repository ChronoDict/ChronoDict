"""Parity checks for NativeEngine and the public ChronoDict backend."""
import importlib.util
import random
import sys
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch
from chronodict import ChronoDict

if importlib.util.find_spec('chronodict._native') is not None:
    from chronodict import _native
    from chronodict._native import NativeEngine
else:
    NativeEngine = None


class BackendSelectionTests(unittest.TestCase):
    def test_selection_without_extension(self):
        with patch.dict(sys.modules, {'chronodict._native': None}):
            self.assertEqual(ChronoDict().backend, 'python')
            self.assertEqual(ChronoDict(backend='python').backend, 'python')
            with self.assertRaisesRegex(ImportError, 'ChronoDict build with Rust support'):
                ChronoDict(backend='native')
        with self.assertRaisesRegex(ValueError, 'backend'):
            ChronoDict(backend='unknown')


@unittest.skipUnless(NativeEngine is not None, 'native extension not built')
class NativeParityTests(unittest.TestCase):
    def setUp(self):
        self.native = NativeEngine(['score'])
        self.python = ChronoDict(indexes=['score'])

    def put(self, key, value):
        self.python[key] = value
        self.native.put(key, value)
        self.assertEqual(self.native.revision, self.python.revision)

    def test_replay_and_queries(self):
        rng = random.Random(613)
        for _ in range(150):
            key = str(rng.randrange(12))
            if key in self.python and rng.randrange(4) == 0:
                del self.python[key]
                self.native.delete(key)
            else:
                self.put(key, {'score': rng.randrange(10), 'fixed': 7})
        for r in range(self.python.revision + 1):
            self.assertEqual(self.native.snapshot(r), self.python.snapshot(r))
            for k in ('1', '2', 'missing'):
                self.assertEqual(self.native.get(k, r), self.python.get(k, version=r))
            for score in range(10):
                self.assertEqual(self.native.find('score', score, r), self.python.find(score=score, version=r))
            self.assertEqual(self.native.find_range('score', 2, 6, r), self.python.find(score__gte=2, score__lte=6, version=r))

    def test_checkpoints(self):
        for i in range(100):
            self.put('a', {'score': i, 'fixed': 7})
        self.assertGreater(self.native.delta_count(), 90)
        for r in range(1, 101):
            self.assertEqual(self.native.get('a', r), self.python.get('a', version=r))

    def test_rollback_and_deletion(self):
        self.put('a', {'score': 1})
        self.native.delete('a')
        del self.python['a']
        self.native.rollback('a', 1)
        self.python.rollback('a', version=1)
        self.assertEqual(self.native.snapshot(), self.python.snapshot())
        self.assertEqual(self.native.revision, self.python.revision)

    def test_copy_isolation(self):
        value = {'score': 2}
        self.put('a', value)
        value['score'] = 3
        result = self.native.get('a')
        result['score'] = 4
        self.assertEqual(self.native.get('a'), {'score': 2})

    def test_invalid_operations(self):
        with self.assertRaises(KeyError):
            self.native.delete('missing')
        with self.assertRaises(ValueError):
            self.native.snapshot(1)
        with self.assertRaises(KeyError):
            self.native.find('bad', 1)
        with self.assertRaises((TypeError, OverflowError)):
            self.native.put('bad', {'score': 2**100})
        self.assertEqual(self.native.revision, 0)

    def test_concurrent_writes(self):
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(lambda i: self.native.put(str(i), {'score': i}), range(100)))
        self.assertEqual(len(self.native), 100)
        self.assertEqual(self.native.revision, 100)
        self.assertEqual(len(self.native.find_range('score', 0, 99)), 100)

    def test_public_backend_parity(self):
        key = ('team', 1)

        def exercise(data):
            data.create_composite_index('team', 'score')
            data[key] = {'team': 'a', 'score': 1, 'items': []}
            data[2] = {'team': 'a', 'score': 2.5}
            data[key]['items'] += [{'nested': [3]}]
            data.patch(key, score=2.5)
            data.patch(key, score=1)
            del data[key]
            data.rollback(key, version=3)
            with data.transaction() as stage:
                self.assertEqual(stage.backend, data.backend)
                stage.patch(2, score=8)
                self.assertEqual(set(stage.find(score__gte=2.5)), {2})

            def results():
                return [(data.snapshot(version),
                         data.find(score=1, version=version),
                         data.find(team='a', score=2.5, version=version),
                         data.find(score__gte=2.5, version=version))
                        for version in range(data.oldest_revision, data.revision + 1)]

            all_revisions = results()
            data.set_retention(max_revisions=4)
            data.compact()
            self.assertEqual(data.find(at=data.timestamp(), score=1), data.find(score=1))
            return all_revisions, results()

        with patch.object(_native, 'active_posting_keys', wraps=_native.active_posting_keys) as lookup:
            python = ChronoDict(indexes=['score'])
            native = ChronoDict(indexes=['score'], backend='native')
            self.assertEqual(native.backend, 'native')
            expected = exercise(python)
            lookup.assert_not_called()
            self.assertEqual(exercise(native), expected)
            lookup.assert_called()

    def test_branch_and_differential_search_use_native_postings(self):
        with patch.object(_native, 'active_posting_keys', wraps=_native.active_posting_keys) as lookup:
            data = ChronoDict({'a': {'score': 1}}, indexes=['score'], backend='native')
            fork = data.branch('experiment')
            self.assertEqual(fork.backend, 'native')
            fork.patch('a', score=5)
            self.assertEqual(fork.find_changed(1, 2, score__gte=2), {'a': {'score': 5}})
            self.assertEqual(data.merge('experiment'), 2)
            self.assertEqual(data.find_changed(1, 2, score=5), {'a': {'score': 5}})
            self.assertGreaterEqual(lookup.call_count, 2)


if __name__ == '__main__':
    unittest.main()
