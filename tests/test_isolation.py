"""Phase 4.2: committed-read isolation and exclusive writer admission."""
import unittest
from concurrent.futures import ThreadPoolExecutor
from chronodict import ChronoDict


class IsolationTests(unittest.TestCase):
    def test_readers_see_committed_state(self):
        d = ChronoDict({1: {'score': 10}}, indexes=['score'])
        with ThreadPoolExecutor(max_workers=1) as pool:
            with d.transaction() as tx:
                tx.patch(1, score=20)
                tx[2] = {'score': 30}
                self.assertEqual(pool.submit(d.snapshot).result(timeout=3), {1: {'score': 10}})
                self.assertEqual(pool.submit(d.find, score__gt=15).result(timeout=3), {})
                self.assertEqual(pool.submit(d.get, 1).result(timeout=3), {'score': 10})
                self.assertEqual(tx.get(1), {'score': 20})
            self.assertEqual(pool.submit(d.snapshot).result(timeout=3), {1: {'score': 20}, 2: {'score': 30}})

    def test_competing_writes_rejected(self):
        d = ChronoDict({1: {'score': 10}})
        def compete():
            with d.transaction():
                pass
        with ThreadPoolExecutor(max_workers=1) as pool:
            with d.transaction() as tx:
                for fn, args in [(d.patch, (1,)), (d.compact, ()), (compete, ())]:
                    with self.assertRaises(RuntimeError):
                        pool.submit(fn, *args).result(timeout=3)
                tx.patch(1, score=20)
            pool.submit(d.patch, 1, score=30).result(timeout=3)
        self.assertEqual(d.revision, 3)
        self.assertEqual(d.get(1), {'score': 30})

    def test_abort_releases_writer_admission(self):
        d = ChronoDict({1: 10})
        with self.assertRaises(ValueError):
            with d.transaction() as tx:
                tx[1] = 20
                raise ValueError('abort')
        with d.transaction() as tx:
            self.assertEqual(tx[1], 10)
            tx[1] = 30
        self.assertEqual(d[1], 30)
        self.assertEqual(d.revision, 2)
