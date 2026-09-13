import ast
import json
from pathlib import Path
import unittest

from automatic_stratagems.scanner import stratagem_detection as detection

SCANNER = Path(__file__).resolve().parents[1] / 'scanner'
DECLARED = {'game_reference_top3', 'colorless_top3', 'name_top3', 'shortlist_top3',
            'detail_top3', 'feature_top3', 'correlation_top3', 'silhouette_top3'}


def cached(value):
    """Round-trip through JSON the way the recognition cache stores results."""
    return json.loads(json.dumps(value))


class RankingDeclarationTests(unittest.TestCase):
    def test_declares_exactly_the_produced_rankings(self):
        from automatic_stratagems.scanner.recognize import match_result
        self.assertEqual(set(match_result.RANKING_KEYS), DECLARED)

    def test_scanner_code_names_rankings_only_through_the_declaration(self):
        literals = []
        for path in sorted(SCANNER.rglob('*.py')):
            if path.name == 'match_result.py':
                continue
            for node in ast.walk(ast.parse(path.read_text(), filename=str(path))):
                if isinstance(node, ast.Constant) and isinstance(node.value, str) \
                        and node.value.endswith('_top3'):
                    literals.append(f'{path.relative_to(SCANNER)}:{node.lineno}: {node.value}')
        self.assertEqual(literals, [])


class RestoreRankingTests(unittest.TestCase):
    def test_restores_pairs_as_tuples(self):
        from automatic_stratagems.scanner.recognize.match_result import restore_ranking
        self.assertEqual(restore_ranking([[.9, 'A'], [1, 'B']]), [(.9, 'A'), (1, 'B')])

    def test_rejects_malformed_rankings(self):
        from automatic_stratagems.scanner.recognize.match_result import restore_ranking
        for value in ('x', None, {'A': 1}, [[float('nan'), 'A']], [[float('inf'), 'A']],
                      [['1', 'A']], [[1, 2]], [[1, 'A', 3]], [[1]], [(1, 'A')], ['A']):
            with self.subTest(value=value):
                self.assertIsNone(restore_ranking(value))

    def test_candidates_length_and_tuple_pairs_are_opt_in(self):
        from automatic_stratagems.scanner.recognize.match_result import restore_ranking
        pairs = [(1, 'A'), [.5, 'B']]
        self.assertEqual(restore_ranking(pairs, pair_types=(list, tuple), min_length=2,
                                         candidates={'A', 'B'}), [(1, 'A'), (.5, 'B')])
        self.assertIsNone(restore_ranking(pairs[:1], pair_types=(list, tuple), min_length=2))
        self.assertIsNone(restore_ranking(pairs, pair_types=(list, tuple), candidates={'A'}))


class RestoreMissionResultTests(unittest.TestCase):
    def mission_result(self):
        return {
            'id': 'A', 'method': 'mission-icon', 'conflict': False,
            'game_reference_top3': [(.95, 'A'), (.5, 'B')],
            'correlation_top3': [(.7, 'A'), (.6, 'B')], 'silhouette_top3': [(.8, 'A'), (.4, 'B')],
            'native_normalized_attempt': {
                'id': 'A', 'feature_top3': [(.96, 'A'), (.8, 'B')],
                'detail_top3': [(.9, 'A'), (.7, 'B')], 'detail_components': {'A': [.9, .9]}},
            'contrast_attempt': {'shortlist_top3': [(.97, 'A'), (.7, 'B')]},
            'colorless_attempt': {'colorless_top3': [(.9, 'A'), (.2, 'B')]},
            'name_fallback': {'name_top3': [(1.0, 'A'), (0.0, 'B')]},
        }

    def test_round_trips_every_declared_ranking(self):
        expected = self.mission_result()
        restored = detection.restore_mission_result(cached(expected), {'A': {}, 'B': {}})
        self.assertEqual(restored, expected)
        self.assertIsInstance(restored['native_normalized_attempt']['feature_top3'][0], tuple)
        self.assertIsInstance(restored['name_fallback']['name_top3'][0], tuple)

    def test_unknown_top3_suffix_is_not_a_ranking(self):
        saved = {**cached(self.mission_result()), 'future_top3': 'not a ranking',
                 'extra': {'other_top3': [['x', 1]]}}
        restored = detection.restore_mission_result(saved, {'A': {}, 'B': {}})
        self.assertIsNotNone(restored)
        self.assertEqual(restored['future_top3'], 'not a ranking')
        self.assertEqual(restored['extra'], {'other_top3': [['x', 1]]})

    def test_malformed_declared_ranking_is_a_miss(self):
        for key in sorted(DECLARED):
            for bad in ('x', [[float('nan'), 'A']], [['1', 'A']], [[1, 'A', 2]]):
                with self.subTest(key=key, bad=bad):
                    saved = {**cached(self.mission_result()), 'nested': {key: bad}}
                    self.assertIsNone(detection.restore_mission_result(saved, {'A': {}, 'B': {}}))


class RestoreEquippedResultTests(unittest.TestCase):
    def test_round_trips_feature_and_shortlist_results(self):
        base = {'id': 'A', 'conflict': False, 'decision': 'threshold',
                'detail_top3': [(.9, 'A'), (.5, 'B')], 'detail_components': {'A': [.9, .9]}}
        for extra in ({'feature_top3': [(.95, 'A'), (.6, 'B')]},
                      {'shortlist_top3': [(.97, 'A'), (.7, 'B')]}):
            with self.subTest(ranking=next(iter(extra))):
                expected = {**base, **extra}
                self.assertEqual(detection.restore_equipped_result(cached(expected), {'A', 'B'}),
                                 expected)

    def test_rejects_short_or_unknown_candidate_rankings(self):
        base = {'id': 'A', 'conflict': False, 'decision': 'threshold', 'detail_components': {}}
        for detail, feature in (([[.9, 'A']], [[.9, 'A'], [.5, 'B']]),
                                ([[.9, 'A'], [.5, 'C']], [[.9, 'A'], [.5, 'B']]),
                                ([[.9, 'A'], [.5, 'B']], 'x')):
            with self.subTest(detail=detail, feature=feature):
                saved = {**base, 'detail_top3': detail, 'feature_top3': feature}
                self.assertIsNone(detection.restore_equipped_result(saved, {'A', 'B'}))


if __name__ == '__main__':
    unittest.main()
