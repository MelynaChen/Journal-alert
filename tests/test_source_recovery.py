import logging
import unittest
from unittest.mock import patch

from jalert.conferences import Client, CoverageUnavailable, collect_conferences
from jalert.fetch import FetchError
from jalert.report import _source_status_section
from jalert.state import Store
from jalert.usenix import ScheduleParser, usenix


PAGE = '''<html><article class="node node-paper"><h2><a href="/conference/fast26/presentation/test">A <em>NAND</em> Flash Study</a></h2>
<div class="field-name-field-paper-people-text"><p>Alice Smith and Bob Chen, <em>University X;</em> Carol Li, <em>Lab Y</em></p></div>
<div class="field-name-field-paper-description-long"><p>Reliable <em>flash</em> storage.</p><p>More details.</p></div></article>
<article class="node node-paper"><h2><a href="/conference/osdi26/presentation/wrong">Wrong venue</a></h2><div class="field-name-field-paper-description-long">NAND</div></article>
<article class="node node-paper"><h2><a href="/conference/fast26/presentation/welcome">Welcome</a></h2></article></html>'''


class SourceRecoveryTests(unittest.TestCase):
    def test_html_is_diagnostic_and_not_cached(self):
        with Store(':memory:') as store, patch('jalert.conferences.http_request', return_value=(200, b'<!DOCTYPE html><title>Anubis</title>')):
            client = Client({'request_interval_seconds': 0}, store)
            with self.assertRaisesRegex(FetchError, 'HTML challenge/access page'):
                client.get('https://dblp.org/search/publ/api')
            self.assertIsNone(store.cached('https://dblp.org/search/publ/api', 12))

    def test_dblp_mirror_recovery_and_circuit_breaker(self):
        with Store(':memory:') as store:
            client = Client({}, store)
            good = {'result': {'hits': {'@total': '0'}}}
            with patch.object(client, 'get', side_effect=[FetchError('HTML challenge'), good, good]) as get:
                self.assertEqual(client.dblp_page({'q': 'test'}), good)
                self.assertEqual(client.dblp_page({'q': 'next'}), good)
                self.assertEqual(get.call_count, 3)
                self.assertIn('dblp.uni-trier.de', get.call_args.args[0])

    def test_no_repeated_requests_after_all_dblp_hosts_fail(self):
        with Store(':memory:') as store:
            client = Client({}, store)
            with patch.object(client, 'get', side_effect=FetchError('unavailable')) as get:
                for _ in range(3):
                    with self.assertRaises(FetchError):
                        client.dblp_page({'q': 'test'})
                self.assertEqual(get.call_count, 3)

    def test_usenix_exact_edition_and_structural_metadata(self):
        parser = ScheduleParser('https://www.usenix.org/conference/fast26/technical-sessions')
        parser.feed(PAGE)
        self.assertEqual(len(parser.records), 1)
        record = parser.records[0]
        self.assertEqual(record['title'], 'A NAND Flash Study')
        self.assertEqual(record['authors'], ['Alice Smith', 'Bob Chen', 'Carol Li'])
        self.assertIn('Reliable flash storage.', record['abstract'])

    def test_usenix_retains_partial_results_and_does_not_complete_scan(self):
        # A failed older edition must not discard papers from the working edition.
        from datetime import date
        current = date.today().year
        page = PAGE.replace('fast26', f'fast{current % 100:02d}')
        cfg = {'conferences': {'enabled': True, 'sources': ['usenix'], 'rescan_years': 1,
                               'request_interval_seconds': 0, 'venues': [{'name': 'FAST', 'usenix_slug': 'fast'}]}}
        with Store(':memory:') as store, patch('jalert.usenix.http_request', side_effect=[(200, page.encode()), FetchError('HTTP 503')]):
            items, statuses = collect_conferences(cfg, store, logging.getLogger('test'))
            self.assertEqual(len(items), 1)
            self.assertEqual(items[0].date, '')
            self.assertFalse(statuses[0]['complete'])
            store.complete_scans(statuses)
            self.assertFalse(store.scanned('FAST', 'usenix'))

    def test_report_distinguishes_failures_coverage_and_counts(self):
        statuses = [
            {'journal': 'Journal', 'source': 'rss', 'ok': True, 'items': 1},
            {'journal': 'FAST', 'paper_type': 'conference', 'source': 'dblp', 'ok': False, 'items': 0, 'note': 'HTML'},
            {'journal': 'FAST', 'paper_type': 'conference', 'source': 'openalex', 'ok': False, 'unavailable': True, 'items': 0, 'note': 'no source'},
        ]
        text = '\n'.join(_source_status_section(statuses, 'summary'))
        self.assertIn('1 本期刊、1 个会议', text)
        self.assertIn('**1** 条通道请求失败', text)
        self.assertIn('**1** 条缺少', text)
        self.assertIn('本次无可用来源：FAST', text)


if __name__ == '__main__':
    unittest.main()
