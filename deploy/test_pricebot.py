import datetime as dt
import importlib.machinery
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import cellarion_pricebot as p


def load(name, file):
    loader = importlib.machinery.SourceFileLoader(name, str(Path(__file__).with_name(file)))
    spec = importlib.util.spec_from_loader(name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


cli = load('cli', 'cellarion-cli')
runner = load('runner', 'pricebot')
URL = 'https://www.supervin.dk/example-2020'


def offer(price=200, url=URL, currency='DKK'):
    return dict(name='Producer Cuvee 2020 75 cl', price=price, url=url, currency=currency, kind='jsonld')


class PricebotTests(unittest.TestCase):
    def test_source_gate(self):
        for url in ('https://dba.dk/a', 'https://wine-searcher.com/a', 'https://vivino.com/a',
                    'https://supervin.dk.evil.com/a', 'https://supervin.dk/auction/a',
                    'https://supervin.dk/blog/a', 'http://supervin.dk/a'):
            self.assertFalse(p.allowed(url), url)
        self.assertTrue(p.allowed(URL))

    def test_structured_product_only(self):
        page = '<script type="application/ld+json">' + json.dumps({'@type': 'Product', 'name': 'Wine',
            'offers': {'@type': 'Offer', 'price': '1.234,50', 'priceCurrency': 'DKK'}}) + '</script>'
        self.assertEqual(p.extract(page, URL)[0]['price'], 1234.5)
        self.assertEqual(p.extract('Wine costs 200 kr', URL), [])
        self.assertEqual(p.extract(page, 'https://dba.dk/a'), [])
        self.assertEqual(p.extract(page.replace('Offer"', 'AggregateOffer"'), URL), [])
        self.assertEqual(p.extract(page.replace('"priceCurrency"', '"unknownCurrency"'), URL), [])

    def test_meta_order_and_currency(self):
        page = '<title>Wine</title><meta content="100" property="product:price:amount"><meta property="product:price:currency" content="EUR">'
        self.assertEqual(p.extract(page, URL)[0]['currency'], 'EUR')
        self.assertEqual(p.extract(page.replace('EUR', '?'), URL), [])

    def test_median_distinct_shops_and_vintage(self):
        c = [offer(100), offer(999), offer(300, 'https://jyskvin.dk/wine'), offer(200, 'https://juuls.dk/wine')]
        matches = [dict(index=i, vintage='2020', ml=750, bottles=1) for i in range(4)]
        result = p.choose(c, matches, '2020')
        self.assertEqual(result['price_dkk'], 200)
        self.assertEqual(len(result['offers']), 3)
        self.assertIn('median 3 shops', result['source'])
        self.assertEqual(json.loads(result['somm_notes'])['spread_dkk'], [100, 300])

    def test_nearest_and_foreign_conversion(self):
        c = [offer(100, 'https://vineshop24.de/wine', 'EUR'), offer(500)]
        m = [dict(index=0, vintage='2020', ml=750, bottles=1), dict(index=1, vintage='2019', ml=750, bottles=1)]
        self.assertEqual(p.choose(c, m, '2020')['price_dkk'], 500)  # Danish first
        self.assertEqual(p.choose(c[:1], m[:1], '2020')['price_dkk'], 746)
        self.assertIsNone(p.choose(c, [dict(index=500)], '2020'))
        self.assertIsNone(p.choose(c, [dict(index=0, vintage='2020', ml=750, bottles=6)], '2020'))

    def test_invoice_protection_even_missing_search_id(self):
        def api(tool, args=None):
            if tool == 'get_wine':
                return {'data': {'name': 'Wine', 'producer': 'Producer'}}
            if tool == 'get_bottle':
                return {'data': {'price': 100}}
            return {'data': [{'bottle_id': 'b', 'wine': {'name': 'Wine', 'producer': 'Producer'}, 'price': None}], 'page': {'total': 1}}
        with patch.object(cli, 'price_mcp', side_effect=api):
            self.assertTrue(cli.invoice_present('wine'))

    def test_write_currency_journal_and_credential(self):
        with patch.object(cli, 'invoice_present', return_value=False), patch.object(cli, 'price_mcp', return_value={'data': {'price_entry_id': 'p'}}) as rpc, patch.object(cli, 'journal_add') as journal:
            cli.set_price('wine', '2020', 200, 'skøn: supervin.dk 2026-10-20')
            self.assertEqual(rpc.call_args.args[1]['currency'], 'DKK')
            self.assertEqual(journal.call_args.kwargs['credential'], 'pricebot')
        with patch.object(cli, 'invoice_present', return_value=True), patch.object(cli, 'price_mcp') as rpc:
            with self.assertRaises(SystemExit):
                cli.set_price('wine', '2020', 200, 'skøn: test')
            rpc.assert_not_called()
        with patch.object(cli, 'mcp') as rpc:
            with self.assertRaises(SystemExit):
                cli.price_mcp('consume_bottle')
            rpc.assert_not_called()

    def test_staleness_and_complete_export(self):
        now = dt.datetime.now(dt.timezone.utc)
        self.assertTrue(runner.due(None, now))
        self.assertFalse(runner.due({'set_at': (now - dt.timedelta(days=180)).isoformat()}, now))
        self.assertTrue(runner.due({'set_at': (now - dt.timedelta(days=181)).isoformat()}, now))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'prices.json'
            path.write_text(json.dumps({'as_of': now.isoformat(), 'complete': False, 'prices': []}))
            with self.assertRaises(ValueError):
                runner.latest_prices(path)

    def test_undo_ignores_lookup_and_uses_pricebot_token(self):
        rows = [{'tool': 'set_vintage_price', 'credential': 'pricebot'},
                {'tool': 'price_lookup', 'read_only': True}]
        previous = cli.TOKEN_FILE
        try:
            with patch.object(cli, 'journal_read', return_value=rows), patch.object(cli, 'journal_write') as write, patch.object(cli, 'mcp', return_value={'data': {'undone': 'set_vintage_price'}}) as rpc:
                with self.assertRaises(SystemExit):
                    cli.main(['undo-last'])
                self.assertEqual(cli.TOKEN_FILE, cli.PRICE_TOKEN)
                rpc.assert_called_once_with('undo_last')
                write.assert_called_once_with([])
        finally:
            cli.TOKEN_FILE = previous

    def test_outage_is_not_unknown(self):
        response = unittest.mock.MagicMock()
        response.__enter__.return_value.read.return_value = b'{"results":[],"unresponsive_engines":[["mojeek","Suspended"]]}'
        with patch.object(p.request, 'urlopen', return_value=response):
            with self.assertRaises(RuntimeError):
                p.search('wine')

    def test_bands(self):
        self.assertEqual(p.band(None), 'ukendt')
        self.assertEqual(p.band(124), '<125')
        self.assertEqual(p.band(125), '125–200')
        self.assertEqual(p.band(2501), '>2500')


if __name__ == '__main__':
    unittest.main()
