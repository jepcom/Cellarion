"""Monthly public catalogue crawler and local product index. Stdlib only."""
import datetime as dt
from concurrent.futures import ThreadPoolExecutor
import hashlib
import html
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import socket
import subprocess
import tempfile
import threading
import time
import unicodedata
import urllib.error
import urllib.parse as urls
import urllib.request as request

UA = 'CellarionPricebot/1.0 (private wine catalogue research; low volume)'
STATE = Path(os.environ.get('CELLARION_STATE_DIR', Path.home() / '.local/state/cellarion'))
DEFAULT_CATALOGUE = STATE / 'catalogue.json'
CONFIG = Path(__file__).with_name('pricebot-shops.json')
CONFIGURED = json.loads(CONFIG.read_text())['shops']
SHOPS = {s['domain']: s['platform'] for s in CONFIGURED if s.get('enabled', True)}
STOP = set('domaine chateau bodegas bodega maison tenuta cantina casa vinicola vineyards vineyard winery fattoria azienda agricola weingut cave caves cellier les le la de du des di del della et and fils pere freres soeur the of von van y e da do dos das'.split())
GENERIC = set('vin wine wines rouge blanc rosso bianco tinto brut nv reserve riserva cuvee red white sec doc docg aoc igt vieilles vignes old vines cuvée'.split())


def norm(text):
    text = unicodedata.normalize('NFKD', text or '').encode('ascii', 'ignore').decode().lower()
    return re.sub(r'[^a-z0-9]+', ' ', text).strip()


def tokens(text, drop=()):
    return {t for t in norm(text).split() if len(t) >= 3 and not t.isdigit() and t not in STOP and t not in drop}


def domain(url):
    return (urls.urlsplit(url).hostname or '').lower().removeprefix('www.')


def allowed_product(url):
    p = urls.urlsplit(url)
    return (p.scheme == 'https' and not p.username and not p.password and p.port in (None, 443)
            and domain(url) in SHOPS and bool(p.path.strip('/'))
            and not re.search(r'auction|auktion|/blog|/news|/review|/search', p.path, re.I))


def amount(value):
    text = str(value).strip().replace(' ', '').replace('\u00a0', '')
    if ',' in text:
        text = text.replace('.', '').replace(',', '.')
    n = float(text)
    if not math.isfinite(n) or n <= 0:
        raise ValueError('invalid positive amount')
    return n


def size_ml(text):
    text = (text or '').lower()
    m = re.search(r'(\d+(?:[.,]\d+)?)\s*(ml|cl|l)\b', text)
    if m:
        return float(m[1].replace(',', '.')) * {'ml': 1, 'cl': 10, 'l': 1000}[m[2]]
    if 'double magnum' in text or 'jeroboam' in text:
        return 3000
    if 'magnum' in text:
        return 1500
    if 'halvflaske' in text or 'halv flaske' in text:
        return 375
    return None


def is_pack(text):
    counts = re.finditer(r'(?i)\b(\d+)\s*(?:stk|flasker|bottles|x\s*\d+(?:[.,]\d+)?\s*(?:cl|ml|l))', text)
    return any(int(m[1]) > 1 for m in counts) or bool(re.search(r'(?i)mixed case|smagekasse|gavekasse', text))


def atomic_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent,
                                         prefix='.' + path.name, delete=False) as stream:
            temporary = Path(stream.name)
            os.fchmod(stream.fileno(), 0o600)
            json.dump(data, stream, ensure_ascii=False, separators=(',', ':'), allow_nan=False)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        if temporary and temporary.exists():
            temporary.unlink()


class Robots:
    """Robots wildcard/end-anchor rules, which stdlib robotparser omits."""
    def __init__(self, text):
        groups = []
        agents, rules, delay = [], [], 1
        has_rules = False
        for line in text.splitlines() + ['User-agent: __end__']:
            line = line.split('#', 1)[0].strip()
            if ':' not in line:
                continue
            key, value = (x.strip() for x in line.split(':', 1))
            key = key.lower()
            if key == 'user-agent':
                if has_rules:
                    groups.append((agents, rules, delay))
                    agents, rules, delay, has_rules = [], [], 1, False
                agents.append(value.lower())
            elif agents and key in ('allow', 'disallow', 'crawl-delay'):
                has_rules = True
                if key == 'crawl-delay':
                    try:
                        delay = max(1, float(value))
                    except ValueError:
                        pass
                elif value:
                    rules.append((key == 'allow', value))
        scored = [(max((len(a) if a != '*' else 0 for a in agents
                       if a == '*' or a in UA.lower()), default=-1), rules, delay)
                  for agents, rules, delay in groups]
        specificity = max((g[0] for g in scored), default=-1)
        chosen = [g for g in scored if g[0] == specificity and specificity >= 0]
        self.rules = [r for _, rules, _ in chosen for r in rules]
        self.delay = max((g[2] for g in chosen), default=1)

    def can_fetch(self, url):
        p = urls.urlsplit(url)
        target = p.path + ('?' + p.query if p.query else '')
        matches = []
        for allow, rule in self.rules:
            end = rule.endswith('$')
            pattern = '^' + re.escape(rule[:-1] if end else rule).replace(r'\*', '.*') + ('$' if end else '')
            if re.search(pattern, target):
                matches.append((len(rule.replace('*', '').rstrip('$')), allow))
        return max(matches, default=(0, True))[1]



class Redirect(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if domain(req.full_url) != domain(newurl):
            raise ValueError('cross-shop redirect refused')
        self.fetcher.validate_url(newurl)
        if urls.urlsplit(newurl).path != '/robots.txt':
            self.fetcher.check_robots(newurl)
        self.fetcher.throttle(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class Fetcher:
    def __init__(self):
        self.last, self.robots = {}, {}
        self.locks = {s: threading.RLock() for s in SHOPS}
        redirect = Redirect()
        redirect.fetcher = self
        self.opener = request.build_opener(redirect)

    def validate_url(self, url):
        p = urls.urlsplit(url)
        if p.scheme != 'https' or p.username or p.password or p.port not in (None, 443) or domain(url) not in SHOPS:
            raise ValueError('unapproved catalogue URL')
        expected = '/products.json' if SHOPS[domain(url)] == 'shopify' else '/wp-json/wc/store/v1/products'
        if p.path not in ('/robots.txt', expected):
            raise ValueError('not a public catalogue endpoint')
        ips = socket.getaddrinfo(p.hostname, 443, type=socket.SOCK_STREAM)
        if not ips or any(not ipaddress.ip_address(x[4][0]).is_global for x in ips):
            raise ValueError('non-public address')

    def throttle(self, url):
        shop = domain(url)
        with self.locks[shop]:
            delay = self.robots[shop].delay if shop in self.robots else 1
            time.sleep(max(0, delay - (time.monotonic() - self.last.get(shop, 0))))
            self.last[shop] = time.monotonic()

    def read(self, url):
        self.validate_url(url)
        with self.locks[domain(url)]:
            self.throttle(url)
            req = request.Request(url, headers={'User-Agent': UA, 'Accept': 'application/json,text/plain'})
            with self.opener.open(req, timeout=30) as response:
                raw = response.read(20_000_001)
                if len(raw) > 20_000_000:
                    raise ValueError('catalogue page too large')
                return raw.decode('utf-8', 'replace')

    def check_robots(self, url):
        shop = domain(url)
        with self.locks[shop]:
            if shop not in self.robots:
                try:
                    body = self.read('https://' + urls.urlsplit(url).netloc + '/robots.txt')
                except urllib.error.HTTPError as exc:
                    if exc.code != 404:
                        raise
                    body = ''
                self.robots[shop] = Robots(body)
            if not self.robots[shop].can_fetch(url):
                raise PermissionError('robots disallows catalogue')

    def get(self, url):
        self.check_robots(url)
        return json.loads(self.read(url))


def parse_products(shop, platform, data):
    if platform == 'shopify':
        if not isinstance(data, dict) or not isinstance(data.get('products'), list):
            raise ValueError('missing Shopify products')
        items = data['products']
    else:
        if not isinstance(data, list):
            raise ValueError('missing WooCommerce products')
        items = data
    rows = []
    for item in items:
        if platform == 'shopify':
            for variant in item.get('variants', []):
                title = html.unescape(item.get('title', ''))
                suffix = variant.get('title') or ''
                if suffix not in ('', 'Default Title'):
                    title += ' [' + suffix + ']'
                try:
                    price = amount(variant.get('price'))
                except (ValueError, TypeError):
                    continue
                rows.append(dict(name=title[:400], vendor=item.get('vendor', '')[:200],
                                 type=item.get('product_type', '')[:150], price=price, currency='DKK',
                                 ml=size_ml(title), available=variant.get('available'),
                                 url='https://' + shop + '/products/' + str(item.get('handle', '')),
                                 row_id=f'{shop}:{item.get("id")}:{variant.get("id")}', kind='shopify'))
        else:
            pr = item.get('prices') or {}
            if pr.get('currency_code') != 'DKK' or pr.get('price_range') or item.get('type') == 'variable':
                continue
            if (item.get('add_to_cart') or {}).get('minimum', 1) > 1:
                continue
            try:
                minor = int(pr['currency_minor_unit'])
                if not 0 <= minor <= 4:
                    continue
                price = amount(pr['price']) / 10 ** minor
            except (KeyError, ValueError, TypeError):
                continue
            title = html.unescape(re.sub('<[^>]+>', '', item.get('name', '')))[:400]
            rows.append(dict(name=title, vendor='', type=' '.join(c.get('name', '') for c in item.get('categories', []))[:150],
                             price=price, currency='DKK', ml=size_ml(title), available=item.get('is_in_stock'),
                             url=item.get('permalink', ''), row_id=f'{shop}:{item.get("id")}', kind='woocommerce'))
    return items, [r for r in rows if allowed_product(r['url']) and domain(r['url']) == shop
                   and r['name'] and not is_pack(r['name']) and (r['ml'] is None or 200 <= r['ml'] <= 6000)]


def crawl_shop(shop, platform, fetcher, max_pages=200):
    rows, fingerprints = [], set()
    started = time.monotonic()
    health = dict(shop=shop, platform=platform, status='error', pages=0, rows=0)
    per_page = 250 if platform == 'shopify' else 100
    for page in range(1, max_pages + 1):
        path = '/products.json' if platform == 'shopify' else '/wp-json/wc/store/v1/products'
        key = 'limit' if platform == 'shopify' else 'per_page'
        url = 'https://' + shop + path + '?' + urls.urlencode({key: per_page, 'page': page})
        try:
            data = fetcher.get(url)
            health['pages'] += 1
            items, parsed = parse_products(shop, platform, data)
            if not items:
                health['status'] = 'ok'
                break
            fingerprint = hashlib.sha256(json.dumps(sorted(str(item.get('id', item.get('handle'))) for item in items)).encode()).hexdigest()
            if fingerprint in fingerprints:
                health['status'] = 'repeated-page'
                break
            fingerprints.add(fingerprint)
            rows.extend(parsed)
            if len(items) < per_page:
                health['status'] = 'ok'
                break
        except PermissionError:
            health['status'] = 'robots-disallowed'
            break
        except Exception as exc:
            health['status'] = 'partial' if rows else 'error'
            health['error'] = type(exc).__name__
            if isinstance(exc, urllib.error.HTTPError):
                health['http_status'] = exc.code
                # Woo uses a documented invalid-page error instead of [] on some versions.
                if platform == 'woo' and exc.code == 400 and rows:
                    try:
                        if json.loads(exc.read()).get('code') in ('woocommerce_rest_product_invalid_page_number', 'rest_post_invalid_page_number'):
                            health['status'] = 'ok'
                    except ValueError:
                        pass
            break
    else:
        health['status'] = 'page-cap'
    health.update(rows_seen=len(rows), rows=len(rows) if health['status'] == 'ok' else 0,
                  seconds=round(time.monotonic() - started, 1))
    # Incomplete or newly robots-disallowed catalogues never become fresh data.
    return (rows if health['status'] == 'ok' else []), health


def refresh_catalogue(path=DEFAULT_CATALOGUE, fetcher=None, workers=8):
    fetcher = fetcher or Fetcher()
    path = Path(path)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(lambda entry: crawl_shop(*entry, fetcher), SHOPS.items()))
    health = [h for _, h in results] + [dict(shop=s['domain'], platform=s['platform'],
             status='excluded-by-policy', rows=0, pages=0, reason=s['reason'])
             for s in CONFIGURED if not s.get('enabled', True)]
    rows = list({r['row_id']: r for rs, _ in results for r in rs}.values())
    as_of = dt.datetime.now(dt.timezone.utc).isoformat()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    history = path.with_name('catalogue-health.jsonl')
    with history.open('a', encoding='utf-8') as stream:
        os.fchmod(stream.fileno(), 0o600)
        for h in health:
            stream.write(json.dumps({'as_of': as_of, **h}) + '\n')
            print(json.dumps(h), flush=True)
    if not rows:
        raise ValueError('no complete nonempty shop catalogue; old cache left intact')
    data = dict(schema_version=1, as_of=as_of, catalogue_id=hashlib.sha256(
        json.dumps(rows, sort_keys=True).encode()).hexdigest()[:16], rows=rows, shops=health)
    atomic_json(path, data)
    return CatalogIndex(data)


class CatalogIndex:
    def __init__(self, data):
        self.as_of = data['as_of']
        self.catalogue_id = data['catalogue_id']
        self.health = data['shops']
        self.rows, self.postings = [], {}
        for row in data['rows']:
            if row.get('available') is False or not allowed_product(row.get('url', '')) or is_pack(row.get('name', '')):
                continue
            try:
                amount(row['price'])
            except (ValueError, TypeError, KeyError):
                continue
            if row.get('currency') != 'DKK' or row.get('kind') not in ('shopify', 'woocommerce'):
                continue
            i = len(self.rows)
            self.rows.append(row)
            for word in tokens(row['name'] + ' ' + row.get('vendor', '') + ' ' + row.get('type', '')):
                self.postings.setdefault(word, set()).add(i)

    @classmethod
    def load(cls, path=DEFAULT_CATALOGUE, now=None):
        data = json.loads(Path(path).read_text())
        stamp = dt.datetime.fromisoformat(data['as_of'].replace('Z', '+00:00'))
        age = (now or dt.datetime.now(dt.timezone.utc)) - stamp
        if data.get('schema_version') != 1 or not dt.timedelta(0) <= age <= dt.timedelta(days=40):
            raise ValueError('catalogue missing/stale; run monthly refresh, never crawl at intake')
        return cls(data)

    def candidates(self, wine, limit=40):
        producer = tokens(wine.get('producer'))
        wanted = tokens(wine.get('name', wine.get('wine', ''))) - producer
        hits = set().union(*(self.postings.get(t, set()) for t in producer)) if producer else set()
        scored = []
        for i in hits:
            row = self.rows[i]
            terms = tokens(row['name'] + ' ' + row.get('vendor', ''))
            ph = len(terms & producer)
            if ph >= max(1, math.ceil(len(producer) / 2)):
                vintage_hit = str(wine.get('vintage') or '') in re.findall(r'\b(?:18|19|20)\d{2}\b', row['name'])
                scored.append((3 * ph + len(terms & wanted) + 2 * vintage_hit, i))
        scored.sort(key=lambda item: (-item[0], self.rows[item[1]]['row_id']))
        return [self.rows[i] for _, i in scored[:limit]]

    def comparables(self, wine, exclude=(), limit=40):
        excluded = set(exclude)
        producer = tokens(wine.get('producer'))
        # Geographic identity comes first; named wine tokens help when registry
        # appellation data is absent. Generic colours/types cannot seed guesses.
        appellation = tokens(wine.get('appellation')) or tokens(wine.get('name', wine.get('wine', '')), GENERIC)
        region = tokens(wine.get('region'), GENERIC)
        producer_hits = set().union(*(self.postings.get(t, set()) for t in producer)) if producer else set()
        place_hits = set().union(*(self.postings.get(t, set()) for t in appellation | region)) if appellation or region else set()
        selected = []
        seen = set()
        for indices, relation, cap in ((producer_hits, 'same_producer', 25), (place_hits, 'appellation_or_region', limit)):
            scored = []
            for i in indices:
                r = self.rows[i]
                if r['row_id'] in excluded or r['url'] in seen:
                    continue
                terms = tokens(r['name'] + ' ' + r.get('vendor', '') + ' ' + r.get('type', ''))
                if relation == 'same_producer' and not (producer and producer <= terms):
                    continue
                place_score = len(terms & appellation) + len(terms & region)
                if relation != 'same_producer' and not (len(terms & appellation) >= min(2, len(appellation)) if appellation else bool(terms & region)):
                    continue
                scored.append((place_score, i))
            scored.sort(key=lambda item: (-item[0], self.rows[item[1]]['row_id']))
            for _, i in scored:
                row = self.rows[i]
                if row['url'] in seen or row['row_id'] in excluded:
                    continue
                selected.append({**row, 'relation': relation})
                seen.add(row['url'])
                if len(selected) >= cap:
                    break
            if len(selected) >= limit:
                break
        return selected[:limit]
