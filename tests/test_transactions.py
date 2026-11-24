"""Phase 4.1: atomic staged transaction regression tests."""
import unittest
from chronodict import ChronoDict


class TransactionTests(unittest.TestCase):
    def setUp(self):
        self.d = ChronoDict(indexes=['score'])
        self.d.create_composite_index('team', 'score')
        self.d[1] = {'team': 'IT', 'score': 10, 'items': []}
        self.d[2] = {'team': 'HR', 'score': 20}

    def test_single_revision(self):
        d = self.d
        with d.transaction() as tx:
            tx.patch(1, score=30)
            tx[1]['items'].append('x')
            del tx[2]
            tx[3] = {'team': 'IT', 'score': 40}
            self.assertEqual(set(tx.find(score__gt=25)), {1, 3})
            self.assertEqual(d.revision, 2)
            self.assertEqual(d.get(1)['score'], 10)
        self.assertEqual(d.revision, 3)
        self.assertEqual(set(d.find(team='IT', score=30)), {1})
        self.assertEqual(set(d.find(score__gt=25)), {1, 3})
        self.assertEqual(set(d.find(score__lt=25, version=2)), {1, 2})
        self.assertEqual(d.get(1)['items'], ['x'])
        self.assertEqual([r['version'] for r in d.history(1)], [1, 3])
        self.assertEqual(d.timestamp(), d.history(3)[0]['timestamp'])
        tx[3]['score'] = 99
        self.assertEqual(d.get(3)['score'], 40)

    def test_exception_aborts(self):
        d = self.d
        before = d.snapshot()
        stamp = d.timestamp()
        with self.assertRaises(TypeError):
            with d.transaction() as tx:
                del tx[2]
                tx.patch(1, score=30)
                tx.patch(1, score=[])
        self.assertEqual(d.snapshot(), before)
        self.assertEqual(d.revision, 2)
        self.assertEqual(d.timestamp(), stamp)
        self.assertEqual(set(d.find(score=20)), {2})
        with d.transaction() as tx:
            tx.patch(1, score=31)
        self.assertEqual(d.revision, 3)

    def test_empty_and_ephemeral_transaction(self):
        with self.d.transaction():
            pass
        with self.d.transaction() as tx:
            tx[3] = None
            del tx[3]
        self.assertEqual(self.d.revision, 2)

    def test_retention_and_index_rebuild(self):
        d = self.d
        d.set_retention(max_revisions=2)
        with d.transaction() as tx:
            tx.patch(1, score=50)
            tx.patch(2, score=60)
        self.assertEqual(d.oldest_revision, 2)
        self.assertEqual(d.revision, 3)
        self.assertEqual(set(d.snapshot(2)), {1, 2})
        d.drop_index('score')
        d.create_index('score')
        self.assertEqual(set(d.find(score__gt=40)), {1, 2})
        self.assertEqual(set(d.find(score__lt=40, version=2)), {1, 2})

    def test_owner_write_and_configuration_rejected(self):
        d = self.d
        with d.transaction() as tx:
            with self.assertRaises(RuntimeError):
                d.patch(1, score=30)
            with self.assertRaises(RuntimeError):
                d.compact()
            with self.assertRaises(RuntimeError):
                tx.drop_index('score')
            with self.assertRaises(RuntimeError):
                with d.transaction():
                    pass
        self.assertEqual(d.revision, 2)

    def test_order_and_stale_views(self):
        d = self.d
        stale = d[1]
        unaffected = d[2]
        with d.transaction() as tx:
            value = tx.get(1)
            del tx[1]
            tx[1] = value
        self.assertEqual(list(d), [2, 1])
        with self.assertRaises(RuntimeError):
            stale['score'] = 9
        unaffected['score'] = 21
        self.assertEqual(d.get(2)['score'], 21)

    def test_commit_failure_not_published(self):
        from unittest.mock import patch
        d = self.d
        original = ChronoDict._commit
        def fail_on_second(owner, key, value, *args, **kwargs):
            if kwargs.get('_batch') and key == 2:
                raise RuntimeError('injected commit failure')
            return original(owner, key, value, *args, **kwargs)
        with patch.object(ChronoDict, '_commit', fail_on_second):
            with self.assertRaises(RuntimeError):
                with d.transaction() as tx:
                    tx.patch(1, score=30)
                    tx.patch(2, score=40)
        self.assertEqual(d.revision, 2)
        self.assertEqual(d.get(1)['score'], 10)
        self.assertEqual(set(d.find(score=10)), {1})
        self.assertEqual(set(d.find(score=30)), set())


if __name__ == '__main__':
    unittest.main()
