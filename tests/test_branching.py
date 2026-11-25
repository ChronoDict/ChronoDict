import unittest
from math import copysign

from chronodict import ChronoDict, MergeConflictError


class BranchTests(unittest.TestCase):
    def test_fork_retained_history_and_independent_indexes(self):
        data = ChronoDict(indexes=['score'], max_revisions=3)
        data.create_composite_index('team', 'score')
        data['row'] = {'score': 1, 'team': 'A', 'items': []}
        data.patch('row', score=2)
        data.patch('row', score=3)
        data['parent-only'] = {'score': 9}

        fork = data.branch('earlier', version=2)
        self.assertEqual(fork.revision, 2)
        self.assertEqual(fork.oldest_revision, 2)
        self.assertEqual(fork.timestamp(), data.timestamp(2))
        self.assertEqual(fork.snapshot(), data.snapshot(2))
        with self.assertRaises(ValueError):
            fork.snapshot(1)

        fork['row']['items'].append('fork')
        fork.patch('row', score=7)
        self.assertEqual(fork.find(team='A', score=7)['row']['items'], ['fork'])
        self.assertEqual(fork.find(score__lt=4, version=2)['row']['items'], [])
        self.assertEqual(data.find(team='A', score=3)['row']['items'], [])
        self.assertEqual(data.find(score=7), {})
        self.assertNotIn('parent-only', fork)

    def test_merge_changes_in_one_revision_and_tracks_previous_merges(self):
        data = ChronoDict({'edit': {'score': 1}, 'remove': None, 'stable': 1}, indexes=['score'])
        fork = data.branch('experiment')
        fork.patch('edit', score=20)
        del fork['remove']
        fork['new'] = {'score': 30}
        data['parent-only'] = 2
        previous = data.revision

        differences = data.compare('main', 'experiment')
        self.assertEqual(set(differences), {'edit', 'remove', 'new', 'parent-only'})
        self.assertTrue(differences['remove']['before_exists'])
        self.assertFalse(differences['remove']['after_exists'])
        differences['edit']['after']['score'] = 100
        self.assertEqual(fork.get('edit'), {'score': 20})

        merged = data.merge('experiment')
        self.assertEqual(merged, previous + 1)
        self.assertEqual(list(data), ['edit', 'stable', 'parent-only', 'new'])
        self.assertEqual(data.find_changed(previous, merged, score__gte=20),
                         {'edit': {'score': 20}, 'new': {'score': 30}})
        self.assertEqual(data.get('edit', version=previous), {'score': 1})
        self.assertEqual(data.merge('experiment'), merged)

        data.patch('edit', score=40)
        fork.patch('new', score=50)
        data.merge('experiment')
        self.assertEqual(data.get('edit'), {'score': 40})
        self.assertEqual(data.get('new'), {'score': 50})

    def test_conflict_is_atomic_and_agreeing_edits_can_merge(self):
        data = ChronoDict({'shared': 1, 'untouched': 2})
        fork = data.branch('experiment')
        data['shared'] = 3
        fork['shared'] = 4
        fork['untouched'] = 5
        revision = data.revision

        with self.assertRaises(MergeConflictError) as captured:
            data.merge('experiment')
        self.assertEqual(captured.exception.keys, ('shared',))
        self.assertEqual(data.revision, revision)
        self.assertEqual(data.snapshot(), {'shared': 3, 'untouched': 2})

        fork['shared'] = 3
        self.assertEqual(data.merge('experiment'), revision + 1)
        self.assertEqual(data.snapshot(), {'shared': 3, 'untouched': 5})

    def test_failed_index_validation_keeps_branch_changes_for_retry(self):
        data = ChronoDict({'row': {'score': 0}}, indexes=['score'])
        fork = data.branch('experiment')
        fork.drop_index('score')
        fork['extra'] = {'score': 1}
        fork.patch('row', score=[])

        with self.assertRaises(TypeError):
            data.merge('experiment')
        self.assertEqual(data.revision, 1)
        self.assertEqual(data.find(score=0), {'row': {'score': 0}})
        self.assertNotIn('extra', data)

        data.drop_index('score')
        self.assertEqual(data.merge('experiment'), 2)
        self.assertEqual(data.snapshot(), fork.snapshot())

    def test_merge_after_fork_point_has_expired(self):
        data = ChronoDict({'row': {'score': 1}}, max_revisions=1)
        fork_revision = data.revision
        fork = data.branch('experiment', at=data.timestamp())
        sibling = data.branch('sibling')
        data['parent-only'] = 1
        fork.patch('row', score=2)
        fork['branch-only'] = 3

        self.assertGreater(data.oldest_revision, fork_revision)
        self.assertGreater(fork.oldest_revision, fork_revision)
        self.assertEqual(set(data.compare('sibling', 'experiment')), {'row', 'branch-only'})
        data.merge('experiment')
        self.assertEqual(data.snapshot(), {'row': {'score': 2}, 'parent-only': 1, 'branch-only': 3})
        self.assertEqual(sibling.snapshot(), {'row': {'score': 1}})

    def test_compare_handles_cycles_and_merge_preserves_signed_zero(self):
        cycle = []
        cycle.append(cycle)
        data = ChronoDict({'cycle': cycle, 'zero': 0.0})
        fork = data.branch('experiment')
        fork['cycle'] = cycle
        fork['zero'] = -0.0
        self.assertEqual(set(data.compare('main', 'experiment')), {'zero'})
        data.merge('experiment')
        value = data.get('cycle')
        self.assertIs(value[0], value)
        self.assertEqual(copysign(1, data.get('zero')), -1)

    def test_names_and_endpoints_are_validated(self):
        data = ChronoDict({'row': 1}, max_revisions=1)
        for name in ('', ' ', 'main', None):
            with self.subTest(name=name), self.assertRaises(ValueError):
                data.branch(name)
        with self.assertRaises(ValueError):
            data.branch('expired', version=0)
        data.branch('existing')
        with self.assertRaises(ValueError):
            data.branch('existing')
        with self.assertRaises(KeyError):
            data.merge('missing')


class DifferentialQueryTests(unittest.TestCase):
    def test_uses_end_state_and_includes_reverted_writes(self):
        data = ChronoDict(indexes=['team', 'score'])
        data.create_composite_index('score', 'team')
        original = {'team': 'A', 'score': 1, 'changed_between': (1, 3)}
        data['reverted'] = original
        data['deleted'] = {'team': 'A', 'score': 1}
        data['cold'] = {'team': 'A', 'score': 1}
        start = data.revision
        data.patch('reverted', score=5)
        data.rollback('reverted', version=start)
        del data['deleted']
        data['added'] = {'team': 'B', 'score': 7}
        end = data.revision
        data.patch('cold', score=10)

        self.assertEqual(data.find_changed(start, end),
                         {'reverted': original, 'added': {'team': 'B', 'score': 7}})
        self.assertEqual(data.find_changed(start, end, team='A', score=1), {'reverted': original})
        self.assertEqual(data.find_changed(start, end, score__gt=1), {'added': {'team': 'B', 'score': 7}})
        self.assertEqual(data.find_changed(start, end, changed_between=(1, 3)), {'reverted': original})
        self.assertEqual(data.find(changed_between=(1, 3)), {'reverted': original})
        self.assertEqual(data.find_changed(end, end), {})

    def test_retention_validation_and_transaction_batch(self):
        data = ChronoDict({'cold': 1, 'row': 1}, max_revisions=2)
        start = data.revision
        with data.transaction() as stage:
            stage['row'] = 2
            stage['added'] = None
            self.assertEqual(stage.find_changed(0, stage.revision), {'row': 2, 'added': None})

        self.assertEqual(data.find_changed(start, data.revision), {'row': 2, 'added': None})
        for invalid in ((0, start), (data.revision, start), (None, start), (start, True), (start, 99)):
            with self.subTest(revisions=invalid), self.assertRaises(ValueError):
                data.find_changed(*invalid)
