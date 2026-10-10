import copy
import json
from pathlib import Path
import tempfile
import unittest

import run
from jalert.cards import parse_report
from jalert.fetch import Item
from jalert.report import build_digest, build_markdown
from jalert.score import build_scorer, Scorer


CONFIG = json.loads((Path(__file__).resolve().parents[1] / 'config.json').read_text(encoding='utf-8'))


class ResearchTests(unittest.TestCase):
    def setUp(self):
        self.scorer = build_scorer(CONFIG)

    def score(self, title, abstract='', paper_type='journal'):
        return self.scorer.score(Item('test', title, abstract=abstract, paper_type=paper_type))

    def test_requested_positive_cases_for_journals_and_conferences(self):
        cases = [
            ('3D NAND retention loss', 'nand_reliability', 20),
            ('SSD garbage collection optimization', 'ftl', 20),
            ('ZNS SSD write amplification', 'nvme', 20),
            ('SSD-based LLM inference', 'ai_storage', 12),
            ('CXL-SSD cache architecture', 'cxl_storage', 12),
        ]
        for kind in ('journal', 'conference'):
            for title, topic, minimum in cases:
                with self.subTest(title=title, kind=kind):
                    scored = self.score(title, paper_type=kind)
                    self.assertFalse(scored.excluded)
                    self.assertGreaterEqual(scored.score, minimum)
                    self.assertIn(topic, scored.topics)
                    self.assertTrue(any(m.topic == topic for m in scored.matches))

    def test_requested_negative_cases(self):
        for title in [
            'Generic neural network training', 'General cloud networking',
            'DRAM-only refresh optimization', 'GC PLC FDP',
            'GC optimization for Java memory management',
            'PLC programmable logic controller reliability',
            'FDP financial optimization', 'FTL faster than light physics',
            'PCM materials for thermal energy storage',
            'Memory storage performance optimization reliability',
            'CXL memory pooling for DRAM management',
            'SSD single shot detector for neural network training',
        ]:
            with self.subTest(title=title):
                self.assertTrue(self.score(title).excluded)

    def test_peripheral_nvm_and_linked_promotion(self):
        for title in ['Phase change memory materials', 'PCM memory cell materials', 'MRAM cell design']:
            result = self.score(title)
            self.assertTrue(result.excluded)
            self.assertEqual(result.relevance, 'peripheral')
            self.assertLessEqual(result.score, 6)
            self.assertEqual(result.topics, ['emerging_nvm'])
        result = self.score('PCM memory cell for SSD storage architecture')
        self.assertFalse(result.excluded)
        self.assertIn('emerging_nvm', result.topics)
        self.assertTrue(any(m.level == 'extended' and m.points == 12 for m in result.matches))

    def test_boundaries_context_distance_and_acronyms(self):
        result = self.score('SSD GCC FTLaunch PLController FDPipeline PCMatic')
        self.assertEqual(result.topics, ['ssd_controller'])
        self.assertEqual(result.score, 15)
        result = self.score('GC ' + 'unrelated discussion ' * 30 + ' SSD')
        self.assertNotIn('ftl', result.topics)
        for title, topic in [('SSD FTL GC', 'ftl'), ('NAND PLC', 'nand_device'), ('SSD FDP', 'nvme')]:
            self.assertIn(topic, self.score(title).topics)
        self.assertNotIn('ftl', self.score('GC tuning', 'SSD measurements').topics)

    def test_title_preference_and_bounded_repetition(self):
        title = self.score('NAND flash')
        abstract = self.score('A study', 'NAND flash')
        repeated = self.score('NAND flash ' * 100, 'NAND flash ' * 100)
        self.assertGreater(title.score, abstract.score)
        self.assertEqual(title.score, repeated.score)
        self.assertEqual(len(repeated.matches), len(title.matches))
        result = self.score('SSD NAND flash retention loss LDPC garbage collection NVMe thermal management SmartSSD secure erase')
        self.assertLessEqual(result.score, CONFIG['research']['max_score'])
        self.assertTrue(set(result.topics).issubset({m.topic for m in result.matches}))

    def test_technical_error_correction_and_editorial_exclusion(self):
        self.assertFalse(self.score('LDPC error correction for NAND flash').excluded)
        self.assertTrue(self.score('Correction: NAND flash retention loss').excluded)

    def test_taxonomy_and_configurable_dram_opt_in(self):
        research = CONFIG['research']
        self.assertEqual(len(research['categories']), 5)
        self.assertEqual(sum(len(c['subtopics']) for c in research['categories']), 16)
        topics = {t.get('topic_id', t['id']) for c in research['categories'] for t in c['subtopics']}
        self.assertEqual(len(topics), 15)
        cfg = copy.deepcopy(CONFIG)
        cfg['research']['contexts']['device'].append('dram')
        cfg['research']['categories'][0]['subtopics'].append({
            'id': 'dram_refresh', 'label': 'DRAM refresh',
            'signals': [{'id': 'dram_refresh', 'level': 'technical', 'context': 'device', 'terms': ['refresh']}],
        })
        result = build_scorer(cfg).score(Item('dram', 'DRAM-only refresh optimization'))
        self.assertFalse(result.excluded)
        self.assertEqual(result.topics, ['dram_refresh'])

    def test_legacy_configuration_unchanged(self):
        cfg = copy.deepcopy(CONFIG)
        cfg.pop('research')
        old = Scorer(cfg['keywords'], cfg['exclude_terms'], cfg['exclude_doi_prefixes'], cfg['conferences']['contextual_keywords'])
        for title in ['NAND flash retention', 'SSD firmware', 'Correction: NAND flash', 'neural network training']:
            item = Item('legacy', title)
            self.assertEqual(build_scorer(cfg).score(item), old.score(item))
            disabled = dict(cfg, research={'enabled': False})
            self.assertEqual(build_scorer(disabled).score(item), old.score(item))

    def test_multitopic_report_cards_digest_and_snapshot(self):
        item = Item('one', 'SSD NAND flash retention loss with CXL memory', journal='Storage Journal',
                    doi='10.1234/example', url='https://example.org/paper')
        scored = self.scorer.score(item)
        self.assertGreater(len(scored.topics), 1)
        entries = [{'item': item, 'scored': scored}]
        restored = run.hydrate_entries([run.entry_to_dict(entries[0])])
        self.assertEqual(restored[0]['scored'].topics, scored.topics)
        self.assertEqual(restored[0]['scored'].category, scored.category)
        report = build_markdown(day='2026-10-10', cfg=CONFIG, entries=restored, statuses=[],
                                fetched_total=1, matched_total=1, generated_at='now')
        self.assertEqual(report.count('### 1.'), 1)
        self.assertIn('**研究方向**', report)
        self.assertIn('**相关性等级**', report)
        self.assertIn('10.1234/example', report)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / '2026-10-10.md'
            path.write_text(report, encoding='utf-8')
            _, cards = parse_report(path)
        self.assertEqual(len(cards), 1)
        self.assertTrue(cards[0]['researchTopics'])
        for limit in (80, 300, 1800):
            _, digest = build_digest(day='2026-10-10', cfg=CONFIG, entries=entries,
                                     fetched_total=1, matched_total=1, max_items=10, max_chars=limit)
            self.assertLessEqual(len(digest), limit)
            self.assertLessEqual(digest.count(item.title), 1)

    def test_old_snapshot_still_renders(self):
        record = {'item': Item('old', 'Old NAND flash paper', url='https://example.org/old').as_dict(),
                  'scored': {'score': 20, 'labels': ['NAND'], 'matches': [
                      {'label': 'NAND', 'term': 'nand', 'field_name': 'title', 'points': 20}]}}
        entries = run.hydrate_entries([record])
        report = build_markdown(day='2026-10-10', cfg=CONFIG, entries=entries, statuses=[],
                                fetched_total=0, matched_total=1, generated_at='now')
        self.assertIn('历史记录（未分类）', report)
        self.assertIn('Old NAND flash paper', report)


if __name__ == '__main__':
    unittest.main()
