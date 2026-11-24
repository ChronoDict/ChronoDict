"""Phase 4.5: deterministic optimistic transaction conflict tests."""
import unittest
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from chronodict import ChronoDict


class ConflictTests(unittest.TestCase):
    def test_competing_transactions(self):
        d = ChronoDict({1: {'score': 0}}, indexes=['score'])
        barrier = Barrier(2, timeout=5)

        def writer(score):
            try:
                with d.transaction(optimistic=True) as tx:
                    tx.patch(1, score=score)
                    barrier.wait()
                return score
            except RuntimeError as exc:
                if 'Transaction conflict' not in str(exc):
                    raise
                return None

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(writer, [10, 20]))
        winners = [v for v in results if v is not None]
        self.assertEqual(len(winners), 1)
        self.assertEqual(d.revision, 2)
        self.assertEqual(d.find(score=winners[0]), {1: {'score': winners[0]}})
        self.assertEqual(d.snapshot(1), {1: {'score': 0}})

    def test_direct_write_conflict_and_retry(self):
        d = ChronoDict({1: 0, 2: 0}, max_revisions=1)
        with self.assertRaisesRegex(RuntimeError, 'Transaction conflict'):
            with d.transaction(optimistic=True) as tx:
                tx[1] = 1
                d[2] = 2
                self.assertEqual(tx[2], 0)
        self.assertEqual(d.snapshot(), {1: 0, 2: 2})
        with d.transaction(optimistic=True) as tx:
            tx[1] = tx[2] + 1
        self.assertEqual(d.snapshot(), {1: 3, 2: 2})
        self.assertEqual(d.oldest_revision, d.revision)

    def test_configuration_and_nested_rejection(self):
        d = ChronoDict({1: 0})
        with d.transaction(optimistic=True) as tx:
            with self.assertRaises(RuntimeError):
                d.create_index('score')
            with self.assertRaises(RuntimeError):
                with d.transaction(optimistic=True):
                    pass
            with self.assertRaises(RuntimeError):
                with d.transaction():
                    pass
            tx[1] = 1
        d.create_index('score')
        self.assertEqual(d[1], 1)

    def test_abort_and_empty(self):
        d = ChronoDict({1: 0})
        with self.assertRaises(ValueError):
            with d.transaction(optimistic=True) as tx:
                tx[1] = 2
                raise ValueError('abort')
        with d.transaction(optimistic=True):
            pass
        self.assertEqual(d.revision, 1)
        with d.transaction() as tx:
            tx[1] = 3
        self.assertEqual(d[1], 3)

    def test_delete_reinsert_conflict(self):
        d = ChronoDict({1: 0})
        with self.assertRaisesRegex(RuntimeError, 'Transaction conflict'):
            with d.transaction(optimistic=True) as tx:
                tx[1] = 2
                del d[1]
                d[1] = 0
        self.assertEqual(d[1], 0)
        self.assertEqual(d.revision, 3)

    def test_invalid_mode(self):
        with self.assertRaises(TypeError):
            with ChronoDict().transaction(optimistic=1):
                pass
