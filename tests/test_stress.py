"""Phase 4.6: concurrent invariants and opt-in throughput benchmark."""
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from time import perf_counter
import unittest

from chronodict import ChronoDict


class ConcurrencyStressTests(unittest.TestCase):
    def test_atomic_updates_and_historical_indexes(self):
        d = ChronoDict({'counter': {'count': 0}}, indexes=['count'])
        start = Barrier(8, timeout=10)

        def worker():
            start.wait()
            for _ in range(100):
                d.atomic_update('counter', lambda v: {'count': v['count'] + 1})

        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = [pool.submit(worker) for _ in range(8)]
            for future in futures:
                future.result(timeout=30)
        self.assertEqual(d.revision, 801)
        for version in range(1, 802):
            self.assertEqual(d.find(version=version, count=version - 1),
                             {'counter': {'count': version - 1}})

    def test_optimistic_retries_preserve_balance(self):
        d = ChronoDict({'a': 1000, 'b': 0})
        start = Barrier(4, timeout=10)

        def worker():
            start.wait()
            for _ in range(25):
                for attempt in range(500):
                    try:
                        with d.transaction(optimistic=True) as tx:
                            tx['a'] -= 1
                            tx['b'] += 1
                        break
                    except RuntimeError as exc:
                        if 'Transaction conflict' not in str(exc):
                            raise
                else:
                    raise AssertionError('retry budget exhausted')

        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = [pool.submit(worker) for _ in range(4)]
            for future in futures:
                future.result(timeout=30)
        self.assertEqual(d.snapshot(), {'a': 900, 'b': 100})
        self.assertEqual(d.revision, 102)
        for revision in range(2, 103):
            self.assertEqual(sum(d.snapshot(revision).values()), 1000)

    def test_readers_during_writes_and_compaction(self):
        d = ChronoDict({'k': {'count': 0}}, indexes=['count'], max_revisions=20)
        start = Barrier(4, timeout=10)

        def writer():
            start.wait()
            for i in range(1, 101):
                d.patch('k', count=i)
                if i % 10 == 0:
                    d.compact()

        def reader():
            start.wait()
            for _ in range(200):
                value = d.snapshot()['k']['count']
                self.assertTrue(0 <= value <= 100)
                revision = d.revision
                try:
                    state = d.snapshot(revision)
                except ValueError:
                    # A writer may prune the captured revision before lookup.
                    self.assertLess(revision, d.oldest_revision)
                else:
                    self.assertEqual(state['k']['count'], revision - 1)

        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = [pool.submit(writer)] + [pool.submit(reader) for _ in range(3)]
            for future in futures:
                future.result(timeout=30)
        self.assertEqual(d.revision, 101)
        self.assertEqual(d.oldest_revision, 82)
        for revision in range(82, 102):
            self.assertEqual(d.find(version=revision, count=revision - 1),
                             {'k': {'count': revision - 1}})


def benchmark(workers, operations=2000):
    """Measure wall-clock throughput; no machine-dependent pass threshold."""
    d = ChronoDict({'counter': 0})
    def worker(count):
        for _ in range(count):
            d.atomic_update('counter', lambda value: value + 1)
    counts = [operations // workers + (i < operations % workers)
              for i in range(workers)]
    start = perf_counter()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(worker, counts))
    elapsed = perf_counter() - start
    if d['counter'] != operations:
        raise AssertionError('benchmark lost updates')
    return elapsed, operations / elapsed


if __name__ == '__main__':
    for workers in (1, 2, 4, 8):
        elapsed, rate = benchmark(workers)
        print(f'{workers} threads: {elapsed:.4f}s, {rate:,.0f} updates/s')
