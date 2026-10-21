"""Catalogue, provenance, vintage and write-safety contracts (no live writes)."""
import datetime as dt
import importlib.machinery
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
import cellarion_catalog as c
import cellarion_pricebot as p


def load(name, file):
    loader = importlib.machinery.SourceFileLoader(name, str(Path(__file__).with_name(file)))
    spec = importlib.util.spec_from_loader(name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


cli = load('cli', 'cellarion-cli')
runner = load('runner', 'pricebot')
exporter = load('exporter', 'cellarion-price-snapshots')
WINE = {'wine_id': 'w', 'producer': 'Producer', 'name': 'Reserve', 'appellation': 'Nuits Saint Georges'}


def row(price=200, shop='bottlehero.dk', name='Producer Reserve 2020', **extra):
    return dict(name=name, vendor='Producer', type='wine', price=price, currency='DKK', ml=750,
                available=True, url=f'https://{shop}/products/{name.replace(" ", "-")}',
                row_id=f'{shop}:{name}', kind='shopify', **extra)


def index(rows):
    return c.CatalogIndex(dict(as_of=dt.datetime.now(dt.timezone.utc).isoformat(), catalogue_id='test', shops=[], rows=rows))


def model_match(i=0, vintage='2020', ml=750):
    return dict(index=i, vintage=vintage, ml=ml, bottles=1)


def shopify_product(i=1):
    return dict(id=i, title='Producer Reserve 2020', vendor='Producer', handle=f'wine-{i}',
                product_type='Wine', variants=[dict(id=i, title='75 cl', price='159.50', available=True)])


class CatalogueTests(unittest.TestCase):
    def test_currency_units_variants_and_sizes(self):
        _, rows = c.parse_products('bottlehero.dk', 'shopify', {'products': [shopify_product()]})
        self.assertEqual(rows[0]['price'], 159.5)
        self.assertEqual(rows[0]['ml'], 750)
        woo = dict(id=1, name='Reserve 0,375 l', permalink='https://vinova.dk/wine', is_in_stock=True,
                   prices=dict(price='15950', currency_code='DKK', currency_minor_unit=2))
        _, rows = c.parse_products('vinova.dk', 'woo', [woo])
        self.assertEqual(rows[0]['price'], 159.5)
        self.assertEqual(rows[0]['ml'], 375)
        _, rows = c.parse_products('vinova.dk', 'woo', [{**woo, 'prices': {**woo['prices'], 'currency_code': 'USD'}}])
        self.assertEqual(rows, [])
        self.assertEqual(c.size_ml('1.5 l'), 1500)
        self.assertEqual(c.size_ml('Magnum'), 1500)
        self.assertEqual(c.size_ml('750ml'), 750)

    def test_robots_wildcards_query_and_bot_specific_rules(self):
        robot = c.Robots('User-agent: *\nDisallow: /*?\nAllow: /public?yes\nDisallow: /private$\n')
        self.assertFalse(robot.can_fetch('https://nichevine.dk/products.json?limit=250'))
        self.assertTrue(robot.can_fetch('https://nichevine.dk/public?yes'))
        self.assertFalse(robot.can_fetch('https://nichevine.dk/private'))
        self.assertTrue(robot.can_fetch('https://nichevine.dk/private-wine'))
        specific = c.Robots('User-agent: *\nDisallow: /\nUser-agent: CellarionPricebot\nAllow: /\nCrawl-delay: 2\n')
        self.assertTrue(specific.can_fetch('https://bottlehero.dk/products.json'))
        self.assertEqual(specific.delay, 2)

    def test_source_and_pack_gate(self):
        for url in ('https://wine-searcher.com/w', 'https://dba.dk/w', 'https://vivino.com/w',
                    'https://bottlehero.dk.evil.test/w', 'https://bottlehero.dk/blog/w', 'http://bottlehero.dk/w'):
            self.assertFalse(c.allowed_product(url), url)
        self.assertTrue(c.is_pack('6 flasker Reserve'))
        self.assertTrue(c.is_pack('6x 75cl'))
        self.assertTrue(c.is_pack('12 x 75 cl'))
        self.assertTrue(c.is_pack('3x20CL'))
        self.assertFalse(c.is_pack('1 x 750 ml'))
        self.assertFalse(c.is_pack('2016 Reserve 750 ml'))
        self.assertEqual(index([row(name='6 flasker Producer Reserve 2020')]).rows, [])

    def test_complete_pagination_and_no_repeated_or_capped_pages(self):
        products = [shopify_product(i) for i in range(250)]
        fake = Mock()
        fake.get.side_effect = [{'products': products}, {'products': []}]
        rows, health = c.crawl_shop('bottlehero.dk', 'shopify', fake)
        self.assertEqual(len(rows), 250)
        self.assertEqual(health['status'], 'ok')
        self.assertEqual(health['pages'], 2)
        fake.get.side_effect = [{'products': products}, {'products': products}]
        rows, health = c.crawl_shop('bottlehero.dk', 'shopify', fake)
        self.assertEqual(rows, [])
        self.assertEqual(health['status'], 'repeated-page')
        fake.get.side_effect = None
        fake.get.return_value = {'products': products}
        rows, health = c.crawl_shop('bottlehero.dk', 'shopify', fake, max_pages=1)
        self.assertEqual(rows, [])
        self.assertEqual(health['status'], 'page-cap')

    def test_partial_failure_never_publishes_partial_prices(self):
        fake = Mock()
        fake.get.side_effect = [{'products': [shopify_product(i) for i in range(250)]}, TimeoutError()]
        rows, health = c.crawl_shop('bottlehero.dk', 'shopify', fake)
        self.assertEqual(rows, [])
        self.assertEqual(health['status'], 'partial')
        self.assertEqual(health['rows_seen'], 250)
        fake.get.side_effect = PermissionError()
        _, health = c.crawl_shop('bottlehero.dk', 'shopify', fake)
        self.assertEqual(health['status'], 'robots-disallowed')

    def test_failed_refresh_preserves_cache_and_logs_health(self):
        fake = Mock()
        fake.get.side_effect = PermissionError()
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory) / 'catalogue.json'
            cache.write_text('old cache')
            with patch.dict(c.SHOPS, {'bottlehero.dk': 'shopify'}, clear=True), patch('builtins.print'):
                with self.assertRaises(ValueError):
                    c.refresh_catalogue(cache, fake)
            self.assertEqual(cache.read_text(), 'old cache')
            health = json.loads(cache.with_name('catalogue-health.jsonl').read_text().splitlines()[0])
            self.assertEqual(health['status'], 'robots-disallowed')

    def test_local_retrieval_and_own_row_exclusion(self):
        cat = index([row(), row(name='Producer Basic 2020'), row(shop='finewines.dk', name='Other Nuits Saint Georges 2021')])
        candidates = cat.candidates(WINE)
        self.assertEqual(candidates[0]['name'], 'Producer Reserve 2020')
        comps = cat.comparables(WINE, exclude=[candidates[0]['row_id']])
        self.assertTrue(any('Basic' in r['name'] for r in comps))
        self.assertTrue(any('Nuits' in r['name'] for r in comps))
        self.assertTrue(all(r['row_id'] != candidates[0]['row_id'] for r in comps))
        self.assertEqual(cat.comparables({'producer': '', 'name': 'wine'}), [])

    def test_stale_cache_and_atomic_private_file(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory) / 'catalogue.json'
            c.atomic_json(cache, dict(schema_version=1, as_of=(dt.datetime.now(dt.timezone.utc)-dt.timedelta(days=41)).isoformat()))
            self.assertEqual(cache.stat().st_mode & 0o777, 0o600)
            with self.assertRaises(ValueError):
                c.CatalogIndex.load(cache)

    def test_shared_per_shop_request_rate_includes_www_alias(self):
        fetcher = c.Fetcher()
        clock = [100.0]
        with patch.object(c.time, 'monotonic', side_effect=lambda: clock[0]), patch.object(c.time, 'sleep', side_effect=lambda n: clock.__setitem__(0, clock[0]+n)) as sleep:
            fetcher.throttle('https://bottlehero.dk/products.json')
            fetcher.throttle('https://www.bottlehero.dk/products.json')
            fetcher.throttle('https://finewines.dk/products.json')
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [0, 1, 0])


class MatchingTests(unittest.TestCase):
    def test_exact_vintage_for_defined_or_old_wines(self):
        for name in ('Colheita Porto', 'Vintage Port', 'Late Bottled Vintage Porto', 'Madeira'):
            self.assertFalse(p.vintage_allowed({'name': name}, '2020', '2021', year=2026))
            self.assertTrue(p.vintage_allowed({'name': name}, '2020', '2020', year=2026))
        self.assertFalse(p.vintage_allowed({'name':'Wine'}, '2009', '2010', year=2026))
        self.assertFalse(p.vintage_allowed({'name':'Wine'}, '2020', '2024', year=2026))
        self.assertTrue(p.vintage_allowed({'name':'Wine'}, '2020', '2023', year=2026))
        self.assertFalse(p.vintage_allowed({'name':'Wine'}, '2020', 'unknown', year=2026))

    def test_median_distinct_shops_and_code_owned_prices(self):
        rows = [row(100), row(300, 'finewines.dk'), row(900)]
        cat = index(rows)
        matches = [model_match(i) | {'price_dkk': 1} for i in range(3)]
        pick = p.choose(WINE, rows, matches, '2020', cat)
        self.assertEqual(pick['price_dkk'], 200)
        self.assertEqual(pick['kind'], 'shop')
        self.assertTrue(pick['source'].startswith('skøn: shops 2 '))
        self.assertEqual(json.loads(pick['somm_notes'])['spread_dkk'], [100, 300])

    def test_unknown_or_hallucinated_vintage_cannot_be_shop_price(self):
        rows = [row()]
        self.assertIsNone(p.choose(WINE, rows, [model_match(vintage='2022')], '2022', index(rows)))
        self.assertIsNone(p.choose(WINE, rows, [model_match(vintage=None)], '2020', index(rows)))
        self.assertEqual(p.validated_matches(WINE, rows, [model_match(100)]), [])

    def test_neighboring_cuvee_and_reserve_tier_rejected(self):
        self.assertFalse(p.identity_matches({'producer':'Producer','name':'Basic'}, row(name='Producer Reserve')))
        self.assertFalse(p.identity_matches({'producer':'Producer','name':'Brunello di Montalcino'}, row(name='Producer Brunello di Montalcino Riserva')))
        self.assertFalse(p.identity_matches({'producer':'Producer','name':'Cuvee Mon Aieul'}, row(name='Producer Reserve Deux Freres')))

    def test_size_comes_from_catalogue_when_known(self):
        half = row()
        half['ml'] = 375
        pick = p.choose(WINE, [half], [model_match(ml=750)], '2020', index([half]))
        self.assertEqual(pick['price_dkk'], 400)

    def test_comparable_estimate_label_references_no_confidence_range(self):
        comps = [{**row(name='Producer Basic 2020'), 'relation':'same_producer'}]
        with patch.object(p, 'ask_json', return_value={'estimate_dkk': 350, 'low': 300, 'high': 400,
                         'basis':'Same producer, higher cuvee level.', 'comparable_indices':[0]}):
            pick = p.estimate(WINE, '2010', comps, index(comps))
        self.assertEqual(pick['kind'], 'estimate')
        self.assertTrue(pick['source'].startswith('estimat: sammenlignelige '))
        notes = json.loads(pick['somm_notes'])
        self.assertTrue(notes['not_a_shop_price'])
        self.assertEqual(notes['cited_comparables'][0]['url'], comps[0]['url'])
        self.assertNotIn('low', pick)
        self.assertNotIn('confidence', notes)

    def test_no_comparables_or_invented_reference_means_unknown(self):
        with patch.object(p, 'ask_json') as ask:
            self.assertIsNone(p.estimate(WINE, '2020', [], index([])))
            ask.assert_not_called()
        comps = [{**row(), 'relation':'same_producer'}]
        with patch.object(p, 'ask_json', return_value={'estimate_dkk': 500, 'basis':'Guess', 'comparable_indices':[40]}):
            self.assertIsNone(p.estimate(WINE, '2020', comps, index(comps)))

    def test_intake_lookup_never_fetches_or_refreshes_catalogue(self):
        cat = index([row()])
        with patch.object(c.Fetcher, 'get', side_effect=AssertionError('no shop requests')), patch.object(c, 'refresh_catalogue', side_effect=AssertionError('no crawl')), patch.object(p, 'ask_json', return_value={'matches':[model_match()]}):
            result = p.lookup(WINE, '2020', cat)
        self.assertEqual(result['kind'], 'shop')

    def test_old_other_vintage_goes_to_comparables_not_shop(self):
        wine = {**WINE, 'name':'Colheita Porto'}
        own = row(name='Producer Colheita Porto 1978')
        other = row(name='Producer Tawny Porto 2000')
        cat = index([own,other])
        def ask(prompt):
            if '"matches"' in prompt:
                candidates = cat.candidates(wine)
                return {'matches':[model_match(next(i for i,r in enumerate(candidates) if r['name']==own['name']), '1978')]}
            return {'estimate_dkk':3500, 'basis':'Aged producer Port comparables, older target vintage.', 'comparable_indices':[0]}
        with patch.object(p, 'ask_json', side_effect=ask):
            result = p.lookup(wine, '1937', cat)
        self.assertEqual(result['kind'], 'estimate')
        self.assertEqual(result['pick']['price_dkk'], 3500)
        self.assertTrue(all(x['row_id'] != own['row_id'] for x in result['comparables']))


class WriteAndSnapshotTests(unittest.TestCase):
    def test_pricebot_cannot_call_consume_tools(self):
        with patch.object(cli, 'mcp') as rpc:
            with self.assertRaises(SystemExit): cli.price_mcp('consume_bottle')
            rpc.assert_not_called()

    def test_invoice_protection_currency_and_journal(self):
        with patch.object(cli, 'invoice_present', return_value=True), patch.object(cli, 'price_mcp') as rpc:
            with self.assertRaises(SystemExit): cli.set_price('w','2020',200,'skøn: shops 1 2026-10-21')
            rpc.assert_not_called()
        with patch.object(cli, 'invoice_present', return_value=False), patch.object(cli, 'price_mcp', return_value={'data':{'price_entry_id':'p'}}) as rpc, patch.object(cli, 'journal_add') as journal:
            cli.set_price('w','2020',200,'skøn: shops 1 2026-10-21')
            self.assertEqual(rpc.call_args.args[1]['currency'], 'DKK')
            self.assertEqual(journal.call_args.kwargs['credential'], 'pricebot')

    def test_estimate_cannot_be_written_without_evidence(self):
        with patch.object(cli, 'price_mcp') as rpc:
            with self.assertRaises(SystemExit): cli.set_price('w','2020',300,'estimat: sammenlignelige 2026-10-21')
            rpc.assert_not_called()
        notes=json.dumps(dict(method='comparables',not_a_shop_price=True,basis='Same producer',cited_comparables=[{'url':'https://finewines.dk/products/basic'}]))
        with patch.object(cli, 'invoice_present', return_value=False), patch.object(cli, 'price_mcp', return_value={'data':{}}) as rpc, patch.object(cli, 'journal_add'):
            cli.set_price('w','2020',300,'estimat: sammenlignelige 2026-10-21',notes)
            self.assertTrue(rpc.call_args.args[1]['source'].startswith('estimat:'))

    def test_undo_ignores_lookup_and_uses_dedicated_token(self):
        rows=[{'tool':'set_vintage_price','credential':'pricebot'},{'tool':'price_lookup','read_only':True}]
        previous=cli.TOKEN_FILE
        try:
            with patch.object(cli,'journal_read',return_value=rows), patch.object(cli,'journal_write') as write, patch.object(cli,'mcp',return_value={'data':{'undone':'set_vintage_price'}}) as rpc:
                with self.assertRaises(SystemExit): cli.main(['undo-last'])
                self.assertEqual(cli.TOKEN_FILE,cli.PRICE_TOKEN)
                rpc.assert_called_once_with('undo_last')
                write.assert_called_once_with([])
        finally: cli.TOKEN_FILE=previous

    def test_180_day_boundary_and_incomplete_export(self):
        now=dt.datetime.now(dt.timezone.utc)
        self.assertTrue(runner.due(None,now))
        self.assertFalse(runner.due({'set_at':(now-dt.timedelta(days=180)).isoformat()},now))
        self.assertTrue(runner.due({'set_at':(now-dt.timedelta(days=181)).isoformat()},now))
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'snapshot.json'
            path.write_text(json.dumps({'as_of':now.isoformat(),'complete':False,'prices':[]}))
            with self.assertRaises(ValueError):runner.latest_prices(path)

    def test_private_snapshot_and_unique_container_read_only(self):
        data={'complete':True,'collection':'winevintageprices','count':0,'prices':[],'as_of':dt.datetime.now(dt.timezone.utc).isoformat()}
        calls=[Mock(stdout='mongo-'+exporter.UUID+'-123\nother-db\n'),Mock(stdout=json.dumps(data))]
        with patch.object(exporter.subprocess,'run',side_effect=calls) as run:
            self.assertEqual(exporter.export(),data)
            self.assertIn('find({},',run.call_args_list[1].args[0][-1])
            self.assertNotIn('update',run.call_args_list[1].args[0][-1])
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'prices.json'
            exporter.write_atomic(path,data)
            self.assertEqual(path.stat().st_mode & 0o777,0o600)

    def test_monthly_fresh_prices_reported_without_model_or_writes(self):
        cat=index([row()])
        fake=Mock()
        groups=[{'wine':WINE,'vintage':'2020','invoice':False},
                {'wine':{**WINE,'wine_id':'invoice'},'vintage':'2022','invoice':True}]
        snapshot={'as_of':dt.datetime.now(dt.timezone.utc).isoformat(),'complete':True,'prices':[
            {'wine_id':'w','vintage':'2020','price':200,'currency':'DKK','source':'skøn: shops 1 2026-10-21',
             'set_at':dt.datetime.now(dt.timezone.utc).isoformat()}]}
        with tempfile.TemporaryDirectory() as directory:
            folder=Path(directory)
            source=folder/'snapshot.json'
            source.write_text(json.dumps(snapshot))
            target=folder/'run.jsonl'
            argv=['pricebot','--apply','--snapshots',str(source),'--output',str(target)]
            with patch.object(runner.sys,'argv',argv), patch.object(runner.CatalogIndex,'load',return_value=cat), patch.object(runner,'load_cli',return_value=fake), patch.object(runner,'inventory',return_value=groups), patch.object(runner,'lookup') as lookup, patch.object(runner,'notify'), patch('builtins.print'):
                self.assertEqual(runner.main(),0)
            lookup.assert_not_called()
            fake.set_price.assert_not_called()
            results=[json.loads(line) for line in target.read_text().splitlines()]
            self.assertEqual(results[0]['action'],'fresh_skipped')
            self.assertEqual(results[1]['kind'],'invoice')
            summary=json.loads(next((folder/'pricebot-runs').glob('*summary.json')).read_text())
            self.assertEqual(summary['counts']['shop_priced'],1)
            self.assertEqual(summary['counts']['written'],0)
            self.assertEqual(summary['counts']['invoice_protected'],1)

    def test_limited_inventory_still_protects_invoice_on_other_vintage(self):
        fake=Mock()
        fake.price_bottles.return_value=[{'bottle_id':'a','wine':{'name':'Wine','producer':'Producer'},'vintage':'2020','price':None},
                                         {'bottle_id':'b','wine':{'name':'Wine','producer':'Producer'},'vintage':'2021','price':100}]
        fake.price_mcp.return_value={'data':{'wine':{'wine_id':'w','name':'Wine','producer':'Producer'}}}
        rows=runner.inventory(fake,include_invoices=True,limit=1)
        self.assertTrue(rows[0]['invoice'])
        fake.price_mcp.assert_called_once_with('get_bottle',{'bottle_id':'a'})


if __name__=='__main__': unittest.main()
