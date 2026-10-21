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
exporter = load('exporter', 'cellarion-price-snapshots')
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
        self.assertEqual(p.choose(c, m, '2020')['price_dkk'], 746)  # Exact vintage first
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

    def test_shopify_prices_and_source_validation(self):
        products = [{'title': 'Wine 2020', 'price': '159.50', 'available': True,
                     'url': '/products/wine-2020', 'vendor': 'Producer'},
                    {'title': 'Unavailable', 'price': '100', 'available': False, 'url': '/products/old'},
                    {'title': 'Injected', 'price': '100', 'url': 'https://dba.dk/wine'},
                    {'title': 'Variable', 'price': '100', 'price_min': '100', 'price_max': '200', 'url': '/products/variable'}]
        c = p.api_candidates('bottlehero.dk', {'resources': {'results': {'products': products}}})
        self.assertEqual(len(c), 1)
        self.assertEqual(c[0]['price'], 159.5)
        self.assertEqual(c[0]['kind'], 'shopify')

    def test_woo_minor_units_currency_and_packs(self):
        base = dict(name='Wine', permalink='https://pinochar.dk/product/wine/', is_in_stock=True,
                    prices={'price': '15950', 'currency_code': 'DKK', 'currency_minor_unit': 2})
        variants = [base, {**base, 'is_in_stock': False},
                    {**base, 'prices': {**base['prices'], 'currency_code': 'USD'}},
                    {**base, 'add_to_cart': {'minimum': 6}}, {**base, 'type': 'variable'}]
        c = p.api_candidates('pinochar.dk', variants)
        self.assertEqual(len(c), 1)
        self.assertEqual(c[0]['price'], 159.5)

    def test_full_hits_still_run_short_query(self):
        response = {'resources': {'results': {'products': [{'title': 'Different Cuvee',
                     'price': '100', 'url': '/products/different'}]}}}
        fetcher = unittest.mock.Mock()
        fetcher.page.return_value = (json.dumps(response), 'https://bottlehero.dk/search/suggest.json')
        candidates, errors = p.shop_candidates('bottlehero.dk', {'producer': 'Domaine Louis Fleurot', 'name': 'Nuits'}, fetcher)
        self.assertEqual(len(candidates), 2)
        self.assertEqual(fetcher.page.call_count, 2)
        self.assertIn('q=Louis+Fleurot', fetcher.page.call_args.args[0])
        self.assertEqual(errors, [])

    def test_robots_wildcards_query_end_and_agent_rules(self):
        r = p.Robots('User-agent: *\nDisallow: /*?\nDisallow: */sog/*\nDisallow: /private$\nAllow: /public?yes\n')
        self.assertFalse(r.can_fetch('https://vildmedvin.dk/catalogsearch/result/?q=wine'))
        self.assertFalse(r.can_fetch('https://kjaersommerfeldt.dk/sog/?q=wine'))
        self.assertFalse(r.can_fetch('https://shop.dk/private'))
        self.assertTrue(r.can_fetch('https://shop.dk/private-product'))
        self.assertTrue(r.can_fetch('https://shop.dk/public?yes'))
        r = p.Robots('User-agent: *\nDisallow: /\nUser-agent: CellarionPricebot\nAllow: /\nCrawl-delay: 2\n')
        self.assertTrue(r.can_fetch(URL))
        self.assertEqual(r.delay, 2)

    def test_snapshot_container_selection_and_atomic_output(self):
        data = {'complete': True, 'collection': 'winevintageprices', 'count': 0, 'prices': [],
                'as_of': dt.datetime.now(dt.timezone.utc).isoformat()}
        responses = [unittest.mock.Mock(stdout='mongo-' + exporter.UUID + '-123\nother-db\n'),
                     unittest.mock.Mock(stdout=json.dumps(data))]
        with patch.object(exporter.subprocess, 'run', side_effect=responses) as run:
            self.assertEqual(exporter.export(), data)
            command = run.call_args_list[1].args[0]
            self.assertEqual(command[:3], ['docker', 'exec', 'mongo-' + exporter.UUID + '-123'])
            self.assertIn('find({},', command[-1])
            self.assertNotIn('update', command[-1])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'prices.json'
            exporter.write_atomic(path, data)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(json.loads(path.read_text()), data)
        with patch.object(exporter.subprocess, 'run', return_value=unittest.mock.Mock(stdout='')):
            with self.assertRaises(ValueError):
                exporter.export()

    def test_unknown_vintage_is_labelled_and_prices_stay_in_code(self):
        c = [offer(150)]
        result = p.choose(c, [{'index': 0, 'vintage': None, 'ml': 750, 'bottles': 1, 'price_dkk': 1}], '2020')
        self.assertEqual(result['price_dkk'], 150)
        self.assertFalse(json.loads(result['somm_notes'])['same_vintage'])
        self.assertEqual(result['offers'][0]['vintage'], 'unknown')

    def test_model_cannot_substitute_neighboring_cuvee(self):
        wine = {'producer': 'Domaine Pierre Usseglio & Fils', 'name': 'Châteauneuf-du-Pape Cuvée de mon Aïeul'}
        wrong = {**offer(999), 'name': 'Pierre Usseglio Châteauneuf-du-Pape Réserve des Deux Frères 2003',
                 'vendor': 'Domaine Pierre Usseglio et Fils'}
        right = {**offer(635), 'name': 'Châteauneuf-Du-Pape Cuvée De Mon Aïeul 2015', 'vendor': 'Domaine Pierre Usseglio'}
        matches = [dict(index=0, vintage='2003', ml=750, bottles=1), dict(index=1, vintage='2015', ml=750, bottles=1)]
        result = p.choose([wrong, right], matches, '2009', wine)
        self.assertEqual(result['price_dkk'], 635)
        self.assertEqual(len(result['offers']), 1)
        self.assertFalse(p.identity_matches({'producer': 'Producer', 'name': 'Special Reserve'},
                                           {'name': 'Producer Basic Wine', 'description': 'Unlike Special Reserve'}))

    def test_lookup_outage_remains_separate_from_unknown(self):
        with patch.object(p, 'gather', return_value=([], [])), patch.object(p, 'fallback', return_value=([], [
                {'stage': 'searxng', 'status': 'search unavailable'}])):
            result = p.lookup({'wine_id': 'w', 'producer': 'Producer', 'name': 'Wine'}, '2020')
            self.assertEqual(result['band'], 'ukendt')
            self.assertTrue(result['search_unavailable'])

    def test_invoice_wines_included_only_for_readonly_validation(self):
        fake = unittest.mock.Mock()
        fake.price_bottles.return_value = [
            {'bottle_id': 'a', 'wine': {'name': 'Wine', 'producer': 'Producer'}, 'vintage': '2020', 'price': 100},
            {'bottle_id': 'b', 'wine': {'name': 'Wine', 'producer': 'Producer'}, 'vintage': '2021', 'price': None}]
        fake.price_mcp.side_effect = lambda tool, args: {'data': {'wine': {'wine_id': 'w', 'name': 'Wine', 'producer': 'Producer'}}}
        self.assertEqual(runner.inventory(fake), [])
        self.assertEqual(len(runner.inventory(fake, include_invoices=True)), 2)

    def test_export_failure_preserves_old_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'prices.json'
            path.write_text('old')
            with patch.object(exporter.sys, 'argv', ['snapshots', '--output', str(path)]), patch.object(exporter, 'export', side_effect=ValueError('failed')):
                self.assertEqual(exporter.main(), 1)
            self.assertEqual(path.read_text(), 'old')

    def test_match_input_filters_identity_and_omits_prices(self):
        wine = {'producer': 'Producer', 'name': 'Reserve'}
        candidates = [{**offer(500), 'name': 'Producer Basic'},
                      {**offer(600), 'name': 'Producer Reserve 2020'}]
        response = unittest.mock.Mock(returncode=0, stdout=json.dumps({'result': json.dumps({
            'matches': [{'index': 1, 'vintage': '2020', 'ml': 750, 'bottles': 1}]})}))
        with patch.object(p.subprocess, 'run', return_value=response) as run:
            self.assertEqual(p.match(wine, candidates)[0]['index'], 1)
            prompt = run.call_args.kwargs['input']
            self.assertNotIn('Producer Basic', prompt)
            self.assertNotIn('"price":', prompt)
            self.assertIn('Producer Reserve', prompt)
        with patch.object(p.subprocess, 'run') as run:
            self.assertEqual(p.match(wine, candidates[:1]), [])
            run.assert_not_called()

    def test_per_shop_rate_state_is_shared(self):
        fetcher = p.Fetcher()
        clock = [100.0]
        def sleep(seconds):
            clock[0] += seconds
        with patch.object(p.time, 'monotonic', side_effect=lambda: clock[0]), patch.object(p.time, 'sleep', side_effect=sleep) as wait:
            fetcher.throttle('https://supervin.dk/a')
            fetcher.throttle('https://www.supervin.dk/b')
            fetcher.throttle('https://juuls.dk/a')
            self.assertEqual([c.args[0] for c in wait.call_args_list], [0, 1, 0])

    def test_basic_wine_cannot_take_reserve_or_premier_cru_offer(self):
        self.assertFalse(p.identity_matches({'producer': 'Livio Sassetti', 'name': 'Brunello di Montalcino'},
                                           {'name': 'Livio Sassetti Brunello di Montalcino Riserva 2012'}))
        self.assertFalse(p.identity_matches({'producer': 'Producer', 'name': 'Nuits-Saint-Georges'},
                                           {'name': 'Producer Nuits-Saint-Georges 1. Cru Les Cailles'}))
        self.assertTrue(p.identity_matches({'producer': 'Producer', 'name': 'Nuits-Saint-Georges 1er Cru Les Cailles'},
                                          {'name': 'Producer Nuits-Saint-Georges 1. Cru Cailles'}))

    def test_limited_dry_run_does_not_resolve_entire_cellar(self):
        fake = unittest.mock.Mock()
        fake.price_bottles.return_value = [
            {'bottle_id': 'a', 'wine': {'name': 'Wine', 'producer': 'Producer'}, 'vintage': '2020', 'price': None},
            {'bottle_id': 'b', 'wine': {'name': 'Wine', 'producer': 'Producer'}, 'vintage': '2021', 'price': 100}]
        fake.price_mcp.return_value = {'data': {'wine': {'wine_id': 'w', 'name': 'Wine', 'producer': 'Producer'}}}
        result = runner.inventory(fake, include_invoices=True, limit=1)
        self.assertEqual(len(result), 1)
        self.assertTrue(result[0]['invoice'])
        fake.price_mcp.assert_called_once_with('get_bottle', {'bottle_id': 'a'})

    def test_bands(self):
        self.assertEqual(p.band(None), 'ukendt')
        self.assertEqual(p.band(124), '<125')
        self.assertEqual(p.band(125), '125–200')
        self.assertEqual(p.band(2501), '>2500')


if __name__ == '__main__':
    unittest.main()
