# Cellarion rough prices: catalogues, shop matches and comparable estimates

Purpose: rough DKK per 750 ml for choosing a bottle, with clear provenance.
This uses ops tooling, public catalogue feeds and Cellarion data/MCP; no app
patch, API billing, Wine-Searcher/Vivino scraping or bottle deletion.

## Monthly pipeline

`cellarion-pricebot.timer` runs on day 1 around 04:15 Europe/Copenhagen.
`ExecStartPre` exports existing price history through the read-only Coolify
Mongo path. The runner then crawls catalogues, loads active wines, protects
invoice wines, and refreshes absent or >180-day-old snapshots. Fresh stored
prices still appear in the coverage report without model calls or writes.
Actual lookup/write attempts are serial, about one wine per two minutes.

Catalogue configuration: `deploy/pricebot-shops.json`. It is based on the
attached tested shop list, not the claim of a particular shop count: the
list contains 41 domains, including a duplicate (laudrupvin.dk, redirected to laudrup.dk) and a food blog
(gastromad.dk). Those two are explicitly excluded, leaving 39 configured
entries. The four exclusions named in the brief (vinoman, ravelinen,
nichevine, callmewine) stay disabled pending review, even if their current
robots response changes. That leaves 35 active feeds. Live robots and catalogue health determine how many are usable.

Shopify `/products.json?limit=250&page=N` supplies decimal **major DKK units**
per variant (unlike Shopify product.js cents). WooCommerce Store API
`/wp-json/wc/store/v1/products?per_page=100&page=N` declares currency and minor
units; only explicit DKK prices are accepted. Every variant is inspected.
Known packs, ambiguous variable prices and invalid sizes are excluded.
Only product URLs on approved store domains can enter the index.

Shops run concurrently with a shared one-request/second limit per shop,
including www redirects; crawl-delay is honored. Robots checks cover each
actual paginated URL and support wildcard/query bans. Absent robots.txt
(404) permits access; blocking, disallow or other errors are not bypassed.
A full catalogue ends on an empty/short page. Repeated pages, page caps,
schema changes and partial downloads are explicit health failures; their
rows are not promoted as fresh data. If all catalogues fail, the old cache
remains intact and the run fails without writes.

`~/.local/state/cellarion/catalogue.json` is an atomic mode-600 snapshot of
complete shop catalogues, including timestamp, generation ID and health.
`catalogue-health.jsonl` appends one dated line per shop/crawl. The index is
loaded into memory with token postings for fast local candidate retrieval.
A cache older than 40 days is rejected. A runner starts a new crawl when
explicitly requested, missing/stale, or from another month. Intake never
starts a crawl or falls back to web search.

## Matching, vintage and arithmetic

Haiku via the existing Claude subscription receives producer/title/vendor
candidates, **without prices**, and selects indices plus vintage/size flags.
It has no tools and is not given API credentials. Code validates its indices,
producer/cuvée/title and release-tier identity, product URLs, sizes, and
vintages; model-generated prices and URLs cannot become shop offers.

For **Colheita, vintage Port (including LBV), Madeira, or a requested vintage
older than 15 years**, only the same vintage is accepted. Other wines allow
at most ±3 years; an unknown vintage cannot stand in for a numbered vintage.
Same-vintage matches take precedence, then nearest vintage. NV ordinary
wines can match explicitly NV or an unnumbered offer. The requested vintage
is always the vintage written to Cellarion.

Prefer 750 ml bottles within the accepted vintage group, otherwise normalize
explicit sizes. When no size is supplied, 750 ml is the default assumption,
marked `size_assumed` in provenance; it must be checked in the human sample. Take each domain's lowest supported matching price, up to
three domains, and store their median. Provenance:

- Shop estimate: `source = skøn: shops <n> <date>`; notes include URLs, matched
  vintages, original sizes/prices, n, spread and catalogue generation.
- Comparable estimate: `source = estimat: sammenlignelige <date>`; notes say
  `not_a_shop_price: true` and include a short basis and cited catalogue rows.
- No supported estimate: `ukendt`; never write a placeholder zero.

Price bands: <125 · 125–200 · 200–300 · 300–450 · 450–700 · 700–1000 ·
1000–1500 · 1500–2500 · >2500. Exact boundaries use the higher band.

## Comparable estimator

Where no valid shop offer remains, retrieve up to 40 catalogue comparables:
other cuvées of the same producer, then related appellation/region products.
Exclude the requested wine's own identified rows across vintages, including
an other-vintage match rejected by the vintage rule. Generic words such as
"wine" and "red" cannot supply comparable evidence by themselves.

The model sees actual normalized comparable prices and returns an estimate,
a one-line basis and indices of evidence used. A finite positive amount,
basis and at least one real cited row are required. No comparables or an
unsupported guess remains unknown. Its low/high output is neither stored
nor presented as a confidence interval: the supplied validation showed
those ranges are poorly calibrated. An estimate remains an approximation;
it is never labelled or counted as a shop quote.

## CLI and intake

```
pricebot --catalogue-only --catalogue ~/.local/state/cellarion/catalogue.json
cellarion price-lookup WINE_ID --vintage 2020
cellarion set-price WINE_ID 2020 DKK_AMOUNT --source PICK_SOURCE --notes PICK_SOMM_NOTES
cellarion undo-last
```

`price-lookup` matches/estimates against the existing cache only. It prints
kind (`shop`, `estimate`, `unknown`), band, provenance and evidence. At intake,
Cleo copies the pick's amount/source/notes to `set-price` after `add --apply`;
if the cache is unavailable, report the failure without redoing the intake.
Do not trigger a crawl for each bottle. See `deploy/INTAKE.md`.

Writes use only `~/.config/cellarion/token-pricebot`, owner-only mode 600,
and pass `currency: DKK` explicitly. The current token is named Claude and
also has consume scope; pricebot's tool allowlist excludes consume tools.
The server enforces write scope and somm/admin role. Before a write, any
invoice price on an active bottle of that wine (including zero) protects
all vintages. The bottle's own price is never changed. Comparable writes
require explicit basis and catalogue-reference notes.

Each write journals the snapshot ID and credential. Lookup journal entries
are read-only and excluded from undo. `undo-last` selects the credential for
the newest journalled write; batch undo switches per action. Undo is
account-wide and time limited, so perform the controlled live test only
immediately after a known price write, before other clients write.

## History, reports and existing monthly review

`cellarion-price-snapshots` resolves exactly one running
`mongo-spuypiijq4qrcd0mzkfpm0qd-*` container, then reads the entire
`winevintageprices` collection with `mongosh find({})`, using the existing
backup access path. It validates the full export before atomically replacing
a private file. No password or new API auth path is needed. Failure leaves
an old file intact but exits nonzero, prevents the service from starting,
and sends one Telegram failure line. The runner rejects incomplete or
>24-hour-old history. It never infers history from the local write journal.

The JSONL run report distinguishes shop, comparable, unknown, invoice and
fresh-skip records. It records original evidence, basis, catalogue generation
and >25% changes. Every run archives its JSONL and a summary under
`pricebot-runs/<UTC timestamp>*`, retaining whole-cellar coverage and shop
health for comparisons. The latest report remains `pricebot-monthly.jsonl`.
The monthly Telegram summary separates shop-priced / estimated / unknown /
failures / changed >25%. Historical FX uses rough fixed rates (EUR 7.46,
SEK .66, NOK .64, USD 6.9); unconvertible histories are counted separately.
No write retries: a timeout may already have committed a snapshot.

The **existing** Multica autopilot "Monthly wine price review" runs day 2,
09:00 Europe/Copenhagen, assigned to DevOps in create_issue mode. Do not
create a second timer/autopilot. Its prompt asks for coverage trend, five
shop plus three estimate checks, catalogue health, proposals, Telegram and
Basic Memory. It stops early until the timer is installed. Reports above
are its evidence; code changes do not modify that autopilot's instructions.

## DevOps handoff

After review/merge, install these files beside each other in `~/ops/bin`:
`cellarion-cli` as `cellarion`, `pricebot`, `cellarion_pricebot.py`,
`cellarion_catalog.py`, `pricebot-shops.json`, `cellarion-price-snapshots`.
Install the units from `deploy/systemd/` into `~/.config/systemd/user/`.
Authenticate the Claude subscription as jb, check Docker read access and
the existing OpenClaw Telegram route. Keep state files private.

First perform a read-only full run and give JB a random **five shop / five
comparable** sample with URLs/basis and vintage/size caveats. After his check,
perform one controlled write/immediate undo, initial writes and activation:

```
systemctl --user daemon-reload
systemctl --user enable --now cellarion-pricebot.timer
systemctl --user list-timers cellarion-pricebot.timer
```

Inspect `journalctl --user -u cellarion-pricebot.service`; disable with
`systemctl --user disable --now cellarion-pricebot.timer`. Do not raise MCP
account rate limits for tests. Limited dry runs bound detail reads; a full
inventory still makes one read per distinct wine/vintage.

Checks: `python3 -m unittest discover -s deploy -p 'test_pricebot.py'` and
`python3 -m unittest discover -s deploy -p 'test_compose.py'`.
