import copy
import json
import logging
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import Mock, patch

import run
from jalert.conferences import Client, collect_conferences, crossref, dblp, make_item, openalex, venue_matches
from jalert.config import load_config
from jalert.fetch import Item, _inverted_abstract, collect_items, parse_feed
from jalert.identity import merge_items
from jalert.report import build_digest, build_markdown
from jalert.score import Scorer
from jalert.score import balance_conferences
from jalert import push as push_module
from jalert.state import Store

ROOT = Path(__file__).resolve().parents[1]
CONFIG = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
FAST = CONFIG["conferences"]["venues"][0]
FAST = {k: v for k, v in FAST.items() if k != "sources"}
LOG = logging.getLogger("tests")


def paper(title="NAND flash reliability", doi="", year=None, venue=FAST):
    return make_item(venue, "dblp", title, year or date.today().year, doi, "https://example.org/paper")


def scorer():
    return Scorer(CONFIG["keywords"], CONFIG["exclude_terms"], CONFIG["exclude_doi_prefixes"], CONFIG["conferences"]["contextual_keywords"])


class MetadataTests(unittest.TestCase):
    def test_venue_aliases_are_exact(self):
        self.assertTrue(venue_matches("24th USENIX Conference on File and Storage Technologies (FAST)", FAST))
        self.assertFalse(venue_matches("Fast Learning Symposium", FAST))
        self.assertFalse(venue_matches("FAST Workshops", FAST))
        micro = next(v for v in CONFIG['conferences']['venues'] if v['name'] == 'MICRO')
        self.assertFalse(venue_matches("Microelectronics Reliability", micro))

    def test_dblp_pages_and_strict_membership(self):
        client = Mock(cfg={"page_size": 1, "max_pages": 3, "rescan_years": 0})
        def hit(key, venue, title):
            return {"info": {"key": key, "venue": venue, "type": "Conference and Workshop Papers", "year": str(date.today().year), "title": title, "authors": {"author": {"text": "A Author"}}}}
        client.dblp_page.side_effect = [
            {"result": {"hits": {"@total": "3", "hit": [hit("conf/fast/a", "FAST", "NAND flash")]}}},
            {"result": {"hits": {"@total": "3", "hit": [hit("conf/fake/a", "FAST", "SSD controller")]}}},
            {"result": {"hits": {"@total": "3", "hit": [hit("conf/fast/b", "FAST Workshops", "SSD firmware")]}}}]
        items = dblp(client, FAST, "2025-01-01", True, LOG)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].date, "")
        self.assertEqual(items[0].authors, ["A Author"])
        self.assertEqual(client.dblp_page.call_args.args[0]["f"], 2)

    def test_crossref_does_not_invent_day(self):
        venue = dict(FAST, crossref_container_titles=["FAST"])
        client = Mock(cfg={"page_size": 2, "max_pages": 2})
        client.get.return_value = {"message": {"items": [{"title": ["NAND flash"], "container-title": ["FAST"], "published": {"date-parts": [[2025]]}, "DOI": "10.1234/ABC"}]}}
        item = crossref(client, venue, "2025-01-01", True, LOG)[0]
        self.assertEqual(item.date, "")
        self.assertEqual(item.year, 2025)
        self.assertEqual(item.doi, "10.1234/abc")
        self.assertIn("container-title%3AFAST", client.get.call_args.args[0])

    def test_openalex_requires_verified_source(self):
        client = Mock(cfg={})
        client.get.return_value = {"results": [{"id": "S1", "display_name": "FAST learning", "type": "journal"}]}
        with self.assertRaises(ValueError):
            openalex(client, FAST, "2025-01-01", True, LOG)

    def test_abstract_inversion(self):
        self.assertEqual(_inverted_abstract({"NAND": [0], "flash": [1, 3], "and": [2]}), "NAND flash and flash")
        self.assertEqual(_inverted_abstract(None), "")

    def test_disabled_has_no_requests(self):
        with patch("jalert.conferences.http_request") as request:
            self.assertEqual(collect_conferences({}, None, LOG), ([], []))
            request.assert_not_called()

    def test_source_failure_does_not_stop_other_source(self):
        cfg = {"conferences": {"enabled": True, "sources": ["dblp", "crossref"], "venues": [FAST]}}
        with Store(":memory:") as store, patch("jalert.conferences.dblp", side_effect=TimeoutError), patch("jalert.conferences.crossref", return_value=[paper()]):
            items, statuses = collect_conferences(cfg, store, LOG)
            self.assertEqual(len(items), 1)
            self.assertFalse(statuses[0]['ok'])
            self.assertTrue(statuses[1]['ok'])
            self.assertTrue(statuses[0]['note'])

    def test_cache_avoids_duplicate_request(self):
        with Store(":memory:") as store, patch("jalert.conferences.http_request", return_value=(200, b'{"results": []}')) as request:
            client = Client({"request_interval_seconds": 0}, store)
            client.get("https://example.org")
            client.get("https://example.org")
            self.assertEqual(request.call_count, 1)

    def test_openalex_doi_enriches_verified_record(self):
        cfg = {'keywords': CONFIG['keywords'], 'conferences': {'enabled': True, 'sources': ['dblp', 'openalex'], 'venues': [FAST]}}
        work = {'doi': 'https://doi.org/10.1234/a', 'publication_date': '2025-03-01', 'abstract_inverted_index': {'NAND': [0], 'flash': [1]}}
        with Store(':memory:') as store, patch('jalert.conferences.dblp', return_value=[paper(doi='10.1234/a')]), patch('jalert.conferences.openalex', return_value=[paper(doi='10.1234/a')]), patch('jalert.conferences.Client.get', return_value=work):
            items, statuses = collect_conferences(cfg, store, LOG)
            self.assertEqual(len(items), 1)
            self.assertEqual(items[0].abstract, 'NAND flash')
            self.assertEqual(items[0].date, '2025-03-01')

    def test_scan_success_failure_and_empty_markers(self):
        with Store(':memory:') as store:
            store.complete_scans([{'journal': 'FAST', 'source': 'dblp', 'ok': False, 'items': 0}])
            self.assertFalse(store.scanned('FAST', 'dblp'))
            store.complete_scans([{'journal': 'FAST', 'source': 'dblp', 'ok': True, 'items': 2}])
            self.assertTrue(store.scanned('FAST', 'dblp'))

    def test_pagination_limit_is_not_empty_success(self):
        client = Mock(cfg={'page_size': 1, 'max_pages': 1, 'rescan_years': 0})
        client.dblp_page.return_value = {'result': {'hits': {'@total': '10', 'hit': []}}}
        with self.assertRaisesRegex(ValueError, 'pagination cap'):
            dblp(client, FAST, '2025-01-01', False, LOG)

    def test_late_previous_year_record_is_not_discarded(self):
        cfg = {'conferences': {'enabled': True, 'sources': ['dblp'], 'venues': [FAST], 'lookback_days': 7}}
        item = paper(year=date.today().year - 1)
        with Store(':memory:') as store, patch('jalert.conferences.dblp', return_value=[item]):
            store.complete_scans([{'journal': 'FAST', 'source': 'dblp', 'ok': True, 'items': 1}])
            items, _ = collect_conferences(cfg, store, LOG)
            self.assertEqual(len(items), 1)
            self.assertFalse(items[0].initial)
            self.assertEqual(items[0].date, '')


class IdentityTests(unittest.TestCase):
    def test_normalized_doi_across_journal_and_conference(self):
        a, b = Item("old", "NAND flash", doi="HTTPS://DOI.ORG/10.1234/ABC"), paper(doi="10.1234/abc")
        self.assertEqual(len(merge_items([a, b])), 1)

    def test_namesakes_distinct_year_venue_doi(self):
        items = [paper(year=2025), paper(year=2026), paper(year=2025, venue={"name": "IEDM"})]
        self.assertEqual(len(merge_items(items)), 3)
        self.assertEqual(len(merge_items([paper(doi="10.1234/a"), paper(doi="10.1234/b")])), 2)

    def test_doi_added_later_does_not_repush(self):
        with Store(":memory:") as store:
            original = paper()
            self.assertFalse(store.observe([original]))
            store.save([(original, scorer().score(original))])
            enriched = paper(doi="https://doi.org/10.1234/NEW")
            self.assertIn(enriched.uid, store.observe([enriched]))
            self.assertEqual(enriched.first_seen, original.first_seen)

    def test_conflicting_doi_not_history_duplicate(self):
        with Store(":memory:") as store:
            a, b = paper(doi="10.1234/a"), paper(doi="10.1234/b")
            store.observe([a]); store.save([(a, scorer().score(a))])
            self.assertFalse(store.observe([b]))

    def test_readonly_does_not_change_database(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "seen.sqlite"
            with Store(path):
                pass
            before = path.read_bytes()
            with Store(path, readonly=True) as store:
                store.observe([paper()])
            self.assertEqual(before, path.read_bytes())


class ScoringTests(unittest.TestCase):
    def test_high_relevance_title_without_abstract(self):
        for title in ("3D NAND reliability", "Improving SSDs using FTL", "NVMe controller design"):
            self.assertGreaterEqual(scorer().score(paper(title)).score, CONFIG['tiers']['min_score'])

    def test_irrelevance_cannot_buy_priority(self):
        item = paper("Fast memory storage performance optimization")
        item.priority = 999
        self.assertEqual(scorer().score(item).score, 0)

    def test_journal_scores_unchanged(self):
        old = Scorer(CONFIG['keywords'], CONFIG['exclude_terms'], CONFIG['exclude_doi_prefixes'])
        for title in ("NAND flash reliability", "Garbage collection in memory", "NVMe", "correction"):
            item = Item("x", title)
            self.assertEqual(old.score(item), scorer().score(item))

    def test_title_has_higher_weight(self):
        a = paper("NAND flash")
        b = paper("A paper"); b.abstract = "NAND flash"
        self.assertGreater(scorer().score(a).score, scorer().score(b).score)

    def test_report_and_push_dates_and_length(self):
        item = paper(); item.authors = ["A Author"]
        entries = [{"item": item, "scored": scorer().score(item)}]
        text = build_markdown(day="2026-10-10", cfg=CONFIG, entries=entries, statuses=[], fetched_total=1, matched_total=1, generated_at="now")
        self.assertIn("**会议**：FAST", text)
        self.assertNotIn("2026-01-01", text)
        _, body = build_digest(day="2026-10-10", cfg=CONFIG, entries=entries * 20, fetched_total=20, matched_total=20, max_items=20, max_chars=180)
        self.assertLessEqual(len(body), 180)

    def test_direction_balancing_only_reorders_qualified_items(self):
        items = [paper('NAND flash A'), paper('NAND flash B'), paper('SSD controller')]
        entries = [{'item': item, 'scored': scorer().score(item)} for item in items]
        balanced = balance_conferences(entries)
        self.assertEqual(balanced[1]['item'].title, 'SSD controller')
        self.assertEqual({id(e) for e in balanced}, {id(e) for e in entries})

    def test_push_secret_is_redacted(self):
        cfg = {'push': {'channels': {'bark': {'enabled': True, 'key': 'secret-key'}}}}
        with patch('jalert.push.push_bark', return_value={'channel': 'bark', 'ok': False, 'detail': 'https://api.day.app/secret-key'}):
            results = push_module.dispatch(cfg, 'title', 'body', LOG)
        self.assertNotIn('secret-key', results[0]['detail'])


class PipelineTests(unittest.TestCase):
    def config(self, root):
        cfg = copy.deepcopy(CONFIG)
        cfg.update(_root=str(root), reports_dir=str(root / 'reports'), log_dir=str(root / 'logs'), state_db=str(root / 'state' / 'seen.sqlite'))
        cfg['output']['cards'] = False
        cfg['conferences']['initial_max_items'] = 1
        return cfg

    def test_complete_pipeline_rerun_and_initial_cap(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = self.config(Path(tmp))
            def conference(*args):
                records = [paper("NAND flash A"), paper("SSD controller B")]
                for item in records:
                    item.initial = True
                return records, [{"journal": "FAST", "source": "dblp", "ok": True, "items": 2}]
            journal = Item("doi:10.1234/j", "NAND flash journal", doi="10.1234/j", date=date.today().isoformat())
            with patch('run.load_config', return_value=cfg), patch('run.collect_items', return_value=([journal], [])), patch('run.collect_conferences', side_effect=conference), patch('run.push_module.dispatch', return_value=[{'ok': True}]) as push:
                self.assertEqual(run.main(['--once']), 0)
                self.assertEqual(run.main(['--once']), 0)
                self.assertEqual(push.call_count, 1)
                snapshot = json.loads(next((Path(tmp) / 'reports').glob('*.json')).read_text(encoding='utf-8'))
                self.assertEqual(len(snapshot['entries']), 2)
            with Store(cfg['state_db']) as store:
                self.assertEqual(store.conn.execute('SELECT COUNT(*) FROM articles').fetchone()[0], 3)
            for handler in logging.getLogger('jalert').handlers:
                handler.close()

    def test_journal_rss_pipeline(self):
        raw = b'<rss><channel><item><title>NAND flash</title><pubDate>Sat, 10 Oct 2026 00:00:00 GMT</pubDate><link>https://example.org/j</link></item></channel></rss>'
        cfg = {'journals': [{'name': 'Journal', 'rss': 'https://example.org/rss'}], 'window': {'sources': {'rss': True, 'openalex': False, 'crossref': False}}}
        with patch('jalert.fetch.http_request', return_value=(200, raw)):
            items, statuses = collect_items(cfg, LOG, since_days=10000)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].paper_type, 'journal')
        self.assertTrue(statuses[0]['ok'])

    def test_disabled_conferences_dry_run_keeps_original_journal(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = self.config(Path(tmp)); cfg['conferences']['enabled'] = False
            item = Item('journal', 'NAND flash', date=date.today().isoformat())
            with patch('run.load_config', return_value=cfg), patch('run.collect_items', return_value=([item], [])), patch('jalert.conferences.http_request') as request, patch('run.push_module.dispatch') as push, patch('builtins.print'):
                self.assertEqual(run.main(['--dry-run']), 0)
                request.assert_not_called(); push.assert_not_called()
                self.assertFalse(Path(cfg['state_db']).exists())
                self.assertEqual(list(Path(cfg['reports_dir']).glob('*.md')), [])
            for handler in logging.getLogger('jalert').handlers:
                handler.close()

    def test_old_snapshot_is_still_renderable(self):
        item = {'uid': 'old', 'title': 'NAND flash', 'journal': 'Journal', 'date': '', 'doi': '', 'url': '', 'abstract': '', 'authors': []}
        entries = run.hydrate_entries([{'item': item, 'scored': {'score': 15}}])
        text = build_markdown(day='2026-10-10', cfg=CONFIG, entries=entries, statuses=[], fetched_total=1, matched_total=1, generated_at='now')
        self.assertIn('**期刊**：Journal', text)


if __name__ == '__main__':
    unittest.main()
