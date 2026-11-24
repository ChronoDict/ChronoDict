"""Phase 4.3: concurrent operations and atomic read-modify-write tests."""
import unittest
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from chronodict import ChronoDict


class ConcurrencyTests(unittest.TestCase):
    def test_atomic_increment(self):
        d = ChronoDict({'counter': {'n': 0}}, indexes=['n'])
        barrier = Barrier(8)
        def worker():
            barrier.wait(timeout=5)
            for _ in range(100):
                d.atomic_update('counter', lambda v: {'n': v['n'] + 1})
        with ThreadPoolExecutor(max_workers=8) as pool:
            for future in [pool.submit(worker) for _ in range(8)]:
                future.result(timeout=15)
        self.assertEqual(d.revision, 801)
        self.assertEqual(d.get('counter'), {'n': 800})
        for n in range(801):
            self.assertEqual(d.find(n=n, version=n+1), {'counter': {'n': n}})

    def test_competing_setdefault(self):
        d = ChronoDict()
        barrier = Barrier(8)
        def worker(n):
            barrier.wait(timeout=5)
            return d.setdefault('key', n)
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(worker, range(8)))
        self.assertEqual(results, [d['key']] * 8)
        self.assertEqual(d.revision, 1)

    def test_concurrent_readers_and_writers(self):
        d = ChronoDict(indexes=['n'])
        barrier = Barrier(6)
        def writer(worker):
            barrier.wait(timeout=5)
            for n in range(50):
                d[(worker, n)] = {'n': n}
        def reader():
            barrier.wait(timeout=5)
            for _ in range(100):
                self.assertTrue(all(v['n'] >= 25 for v in d.find(n__gte=25).values()))
                self.assertTrue(all(k[1] == v['n'] for k,v in d.snapshot().items()))
        with ThreadPoolExecutor(max_workers=6) as pool:
            futures = [pool.submit(writer, i) for i in range(4)] + [pool.submit(reader) for _ in range(2)]
            for f in futures:
                f.result(timeout=15)
        self.assertEqual(len(d), 200)
        self.assertEqual(d.revision, 200)
        self.assertEqual(len(d.find(n__gte=25)), 100)

    def test_callback_failure_and_invalid_index(self):
        d = ChronoDict({1: {'n': 1}}, indexes=['n'])
        def fail(v):
            v['n'] = 99
            raise ValueError('abort')
        with self.assertRaises(ValueError):
            d.atomic_update(1, fail)
        with self.assertRaises(TypeError):
            d.atomic_update(1, lambda v: {'n': []})
        self.assertEqual(d.snapshot(), {1: {'n': 1}})
        self.assertEqual(d.revision, 1)
        result = d.atomic_update(1, lambda v: {'n': 2})
        result['n'] = 100
        self.assertEqual(d.get(1), {'n': 2})

    def test_reentrant_writes_rejected(self):
        d = ChronoDict({1: 1})
        def write(v):
            d[2] = 2
            return v + 1
        for fn in (write, lambda v: d.compact(), lambda v: d.atomic_update(1, lambda x: x)):
            with self.assertRaises(RuntimeError):
                d.atomic_update(1, fn)
        self.assertEqual(d.snapshot(), {1: 1})
        self.assertEqual(d.revision, 1)
        self.assertEqual(d.atomic_update(1, lambda v: v+1), 2)

    def test_transaction_admission_preserved(self):
        d = ChronoDict({1: 1})
        with d.transaction() as tx:
            with self.assertRaises(RuntimeError):
                d.atomic_update(1, lambda v: v+1)
            tx.atomic_update(1, lambda v: v+1)
        self.assertEqual(d[1], 2)
        self.assertEqual(d.revision, 2)


if __name__ == '__main__':
    unittest.main()
