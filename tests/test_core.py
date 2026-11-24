"""Regression tests for the ChronoDict prototype."""
import unittest
from chronodict import ChronoDict


class ChronoDictTests(unittest.TestCase):
    def setUp(self):
        self.d = ChronoDict(indexes=['team', 'salary'])
        self.d[1] = {'team': 'IT', 'salary': 50}
        self.d[2] = {'team': 'HR', 'salary': 60}

    def test_temporal_index(self):
        self.d.patch(1, team='HR')
        self.assertEqual(set(self.d.find(team='HR')), {1, 2})
        self.assertEqual(set(self.d.find(team='IT', version=2)), {1})
        self.assertEqual(self.d.find(team='HR', salary=50), {1: {'team': 'HR', 'salary': 50}})

    def test_delete_restore(self):
        del self.d[1]
        self.assertEqual(self.d.find(team='IT'), {})
        self.assertIn(1, self.d.find(team='IT', version=2))
        self.d.rollback(1, version=1)
        self.assertIn(1, self.d)
        self.assertEqual(self.d.revision, 4)
        self.assertTrue(self.d.history(1)[1]['deleted'])

    def test_copy_isolation(self):
        value = {'team': 'IT', 'nested': []}
        self.d[3] = value
        value['nested'].append(1)
        self.d[3]['nested'].append(2)
        self.assertEqual(self.d[3]['nested'], [])

    def test_snapshot_diff(self):
        self.d.patch(1, salary=80)
        self.assertEqual(self.d.snapshot(1), {1: {'team': 'IT', 'salary': 50}})
        self.assertEqual(self.d.diff(1, from_version=1, to_version=3)['after']['salary'], 80)

    def test_invalid_index_value(self):
        with self.assertRaises(TypeError):
            self.d.patch(1, salary=[])
        self.assertEqual(self.d.revision, 2)
        self.assertEqual(self.d[1]['salary'], 50)

    def test_repeated_intervals(self):
        self.d.patch(1, team='HR')
        self.d.patch(1, team='IT')
        self.assertNotIn(1, self.d.find(team='IT', version=3))
        self.assertIn(1, self.d.find(team='IT', version=4))

    def test_none_and_missing(self):
        self.d[3] = None
        self.assertIsNone(self.d.get(3, 'missing'))
        self.assertEqual(self.d.get(4, 'missing'), 'missing')
        self.assertEqual(self.d.snapshot(0), {})
        with self.assertRaises(ValueError):
            self.d.snapshot(99)

    def test_unindexed_search(self):
        self.d.patch(1, name='Alex')
        self.assertEqual(set(self.d.find(name='Alex')), {1})
        self.assertEqual(self.d.find(name='Alex', version=2), {})
