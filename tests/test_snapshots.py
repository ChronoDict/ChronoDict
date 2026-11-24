"""Concurrent historical snapshot regression tests."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from threading import Event, current_thread
from unittest import TestCase, mock

from chronodict import ChronoDict


class ConcurrentSnapshotTests(TestCase):
    def test_writer_progress_and_pruning_during_snapshot_copy(self):
        d = ChronoDict(indexes=['score'], max_revisions=2)
        d[1] = {'score': 1, 'items': [1]}
        d[2] = {'score': 2}
        revision = d.revision
        timestamp = d.timestamp()
        started, release = Event(), Event()
        main = current_thread()

        def paused_copy(value, *args, **kwargs):
            if current_thread() is not main and not started.is_set():
                started.set()
                if not release.wait(5):
                    raise TimeoutError('snapshot copy was not released')
            return deepcopy(value, *args, **kwargs)

        with ThreadPoolExecutor(max_workers=1) as pool:
            with mock.patch('chronodict.core.deepcopy', side_effect=paused_copy):
                future = pool.submit(d.snapshot, at=timestamp)
                try:
                    self.assertTrue(started.wait(5))
                    d.patch(1, score=3)
                    del d[2]
                    d.compact()
                    self.assertGreater(d.oldest_revision, revision)
                finally:
                    release.set()
                result = future.result(timeout=5)
        self.assertEqual(result, {1: {'score': 1, 'items': [1]}, 2: {'score': 2}})
        result[1]['items'].append(99)
        self.assertEqual(d.get(1), {'score': 3, 'items': [1]})
        with self.assertRaises(ValueError):
            d.snapshot(revision)

    def test_transaction_snapshots_never_see_partial_commit(self):
        d = ChronoDict({'a': 0, 'b': 0})
        staged, release = Event(), Event()

        def writer():
            with d.transaction() as tx:
                tx['a'] = 1
                staged.set()
                if not release.wait(5):
                    raise TimeoutError('transaction was not released')
                tx['b'] = 1

        with ThreadPoolExecutor(max_workers=2) as pool:
            future = pool.submit(writer)
            try:
                self.assertTrue(staged.wait(5))
                self.assertEqual(pool.submit(d.snapshot).result(timeout=5), {'a': 0, 'b': 0})
            finally:
                release.set()
            future.result(timeout=5)
        self.assertEqual(d.snapshot(), {'a': 1, 'b': 1})
        self.assertEqual(d.snapshot(2), {'a': 0, 'b': 0})

    def test_parallel_historical_snapshots(self):
        d = ChronoDict()
        expected = [{}]
        for i in range(1, 81):
            d[i % 5] = {'score': i, 'items': [i]}
            expected.append(d.snapshot())
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(d.snapshot, range(81)))
        self.assertEqual(results, expected)
        results[-1][0]['items'].append(99)
        self.assertEqual(d.snapshot(), expected[-1])

    def test_cyclic_and_aliased_values_remain_isolated(self):
        d = ChronoDict()
        child = []
        value = {'a': child, 'b': child}
        child.append(value)
        d[1] = value
        result = d.snapshot()[1]
        self.assertIs(result['a'], result['b'])
        self.assertIs(result['a'][0], result)
        result['a'].append(1)
        self.assertEqual(len(d.snapshot()[1]['a']), 1)
