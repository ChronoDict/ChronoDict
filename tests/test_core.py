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
        self.assertEqual(self.d[3]['nested'], [2])
        self.assertEqual(self.d.get(3, version=3)['nested'], [])

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


class PhaseTwoRegressionTests(unittest.TestCase):
    """Verify temporal indexes and mutation semantics against independent states."""

    def test_dynamic_index_history(self):
        d = ChronoDict()
        d[1] = {'score': 10}
        d.patch(1, score=20)
        d.create_index('score')
        self.assertEqual(d.find(score=10, version=1), {1: {'score': 10}})
        self.assertEqual(d.find(score__gt=10), {1: {'score': 20}})
        d.drop_index('score')
        self.assertEqual(d.find(score__gt=10), {1: {'score': 20}})
        self.assertEqual(d.revision, 2)

    def test_failed_index_creation(self):
        d = ChronoDict()
        d[1] = {'score': []}
        d.patch(1, score=10)
        with self.assertRaises(TypeError):
            d.create_index('score')
        self.assertNotIn('score', d.indexed_fields)
        self.assertEqual(d.revision, 2)

    def test_range_boundaries(self):
        d = ChronoDict(indexes=['score'])
        for i in range(5):
            d[i] = {'score': i}
        for suffix, expected in [('gt', {3, 4}), ('gte', {2, 3, 4}),
                                 ('lt', {0, 1}), ('lte', {0, 1, 2})]:
            with self.subTest(operator=suffix):
                self.assertEqual(set(d.find(**{'score__' + suffix: 2})), expected)
        self.assertEqual(set(d.find(score__gte=1, score__lt=3)), {1, 2})

    def test_composite_history(self):
        d = ChronoDict(indexes=['score'])
        d[1] = {'team': 'IT', 'country': 'IN', 'score': 10}
        d[2] = {'team': 'IT', 'country': 'US', 'score': 20}
        d.patch(1, country='US')
        d.create_composite_index('team', 'country')
        self.assertEqual(set(d.find(team='IT', country='IN', version=2)), {1})
        self.assertEqual(set(d.find(team='IT', country='US', score__gt=10)), {2})
        d.drop_composite_index('team', 'country')
        self.assertEqual(set(d.find(team='IT', country='US')), {1, 2})

    def test_nested_mutation_and_stale_view(self):
        d = ChronoDict(indexes=['score'])
        d[1] = {'score': 10, 'nested': {'items': [1]}}
        stale = d[1]
        d[1]['nested']['items'].append(2)
        self.assertEqual(d.get(1, version=1)['nested']['items'], [1])
        self.assertEqual(d.get(1)['nested']['items'], [1, 2])
        with self.assertRaises(RuntimeError):
            stale['score'] = 20
        self.assertEqual(d.revision, 2)
        d[1]['score'] = 30
        self.assertEqual(set(d.find(score__gt=20)), {1})
        self.assertEqual(d.find(score__gt=20, version=2), {})

    def test_invalid_nested_mutation_atomicity(self):
        d = ChronoDict(indexes=['score'])
        d[1] = {'score': 10}
        with self.assertRaises(TypeError):
            d[1]['score'] = []
        self.assertEqual(d.revision, 1)
        self.assertEqual(d.find(score=10), {1: {'score': 10}})
        self.assertEqual(len(d.history(1)), 1)

    def test_timestamp_queries(self):
        from datetime import datetime, timezone, timedelta
        from unittest.mock import patch
        d = ChronoDict(indexes=['score'])
        base = datetime.now(timezone.utc) + timedelta(days=1)
        class Clock(datetime):
            current = base

            @classmethod
            def now(cls, tz=None):
                return cls.current

        with patch('chronodict.core.datetime', Clock):
            d[1] = {'score': 10}
            Clock.current = base + timedelta(seconds=2)
            d.patch(1, score=20)
        middle = base + timedelta(seconds=1)
        self.assertEqual(d.revision_at(middle), 1)
        self.assertEqual(d.get(1, at=middle), {'score': 10})
        self.assertEqual(d.snapshot(at=middle), {1: {'score': 10}})
        self.assertEqual(d.find(at=middle, score__lt=15), {1: {'score': 10}})
        self.assertEqual(d.diff(1, from_at=middle, to_version=2)['after'], {'score': 20})
        d.rollback(1, at=middle)
        self.assertEqual(d.get(1), {'score': 10})
        self.assertEqual(d.revision, 3)

    def test_timestamp_validation(self):
        from datetime import datetime
        d = ChronoDict()
        with self.assertRaises(ValueError):
            d.revision_at(datetime(2020, 1, 1))
        with self.assertRaises(TypeError):
            d.revision_at('2020-01-01')
        with self.assertRaises(ValueError):
            d.snapshot(0, at=d.timestamp())

    def test_randomized_temporal_queries(self):
        from copy import deepcopy
        from random import Random
        rng = Random(4102)
        d = ChronoDict(indexes=['score', 'team'])
        d.create_composite_index('team', 'country')
        state = {}
        states = [{}]
        for _ in range(150):
            key = rng.randrange(12)
            if key in state and rng.randrange(4) == 0:
                del state[key]
                del d[key]
            else:
                value = {'score': rng.randrange(20), 'team': rng.choice(['IT', 'HR']),
                         'country': rng.choice(['IN', 'US'])}
                state[key] = value
                d[key] = value
            states.append(deepcopy(state))
        for revision, expected in enumerate(states):
            self.assertEqual(d.snapshot(revision), expected)
            for team in ['IT', 'HR']:
                for country in ['IN', 'US']:
                    for score in [0, 5, 10, 19]:
                        result = {k: v for k, v in expected.items()
                                  if v['team'] == team and v['country'] == country
                                  and v['score'] >= score}
                        self.assertEqual(d.find(version=revision, team=team,
                                                country=country, score__gte=score), result)


if __name__ == '__main__':
    unittest.main()
