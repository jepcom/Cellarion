# Rough retail wine prices (DEV-76)

These ops tools use Cellarion data and MCP, with no application patch. A price
is a retail estimate per 750 ml, never an invoice or a model's guess.
Bands: <125 · 125–200 · 200–300 · 300–450 · 450–700 · 700–1000 ·
1000–1500 · 1500–2500 · >2500 DKK. An exact boundary uses the higher band.

## Commands and credentials

```
cellarion price-lookup WINE_ID --vintage 2020
cellarion set-price WINE_ID 2020 250 --source 'skøn: shop.dk 2026-10-21' --notes '…'
cellarion undo-last
cellarion-price-snapshots --output ~/.local/state/cellarion/price-snapshots.json
pricebot --snapshots ~/.local/state/cellarion/price-snapshots.json --output report.jsonl
```

Lookup prints candidates and the pick without remote writes. Copy the pick's
`price_dkk`, `source` and `somm_notes` into `set-price`. No supported match is
`ukendt`; never substitute a guessed number.

Writes use **only** `~/.config/cellarion/token-pricebot`, owned by jb and mode
600. The current token is named **Claude**, with read+write+consume scopes;
pricebot's tool allowlist excludes consume tools. JB can replace it with
read+write without changing the code. The server enforces somm/admin plus
write scope. DKK is passed explicitly. Any invoice price on an active bottle
of the wine, including zero, protects every vintage from automation.
`Bottle.price` is never changed.

Lookup journals a read-only entry, excluded from undo. Price writes journal
the snapshot ID and credential. `undo-last` selects that credential; batch
undo switches credentials per step. Server undo is account-wide and time
limited: inspect the app if another client may have written since. Test a
live undo only immediately after a controlled price write, never by deleting
bottles. Live write/undo waits for JB's spot check.

## Storefront discovery

The primary path uses these shops' public search endpoints, respecting live
robots rules and a shared limit of one request/second **per shop**. Different
shops run concurrently. Both full producer+wine and shortened-producer
queries always run, including when the first query found another cuvée.

- Shopify suggest API: bottlehero, ottosuenson, theis-vine, finewines,
  sydjyskvin, copenhagenwine, vinpusheren, vininvestoren. The suggest endpoint
  returns major DKK units; do not confuse these with Shopify variant cents.
- WooCommerce Store API: lieu-dit, pinochar, vinova, kokkensvinhus,
  bb-vinimport, vinoman. Convert `prices.price` using `currency_minor_unit`,
  and require explicit DKK. Skip out-of-stock, variable-price and bulk-only
  products.
- Own search pages: Havnens Vin and Excluwine; only their linked product
  pages' JSON-LD Product/Offer or explicit product-price meta is used.
  Search-page text is never a price source. At most four product pages per
  query. The same adapters are configured for Supervin, Kjær & Sommerfeldt
  and VildMedVin, but their search routes are currently disallowed by robots
  (checked 2026-10-21), so those requests are skipped. Do not bypass this by
  trying another route.

Transport allows these exact search endpoints; accepted product URLs still
pass the separate retail-product gate. News/blog/review, auction, Vivino,
Wine-searcher and general marketplace sources remain excluded. Bottlehero's
public product storefront is explicitly authorized by the revised brief.
Redirects remain within reviewed domains and are checked against robots.
Robots matching supports wildcards and `$`, including query-string bans;
stdlib robotparser alone does not implement those rules. Missing robots.txt
(404) permits access; other failures are not bypassed.

SearXNG is a fallback only after no storefront pick. It accepts structured
prices from reviewed `.dk` product pages only. An unavailable search engine
is recorded separately as `search_unavailable`, not a lookup exception and
not evidence that no shops stock a wine.

## Matching and prices

Subscription Haiku selects candidate indices and vintage/size/count flags.
Prices are excluded from its input and ignored in its output. It has no
tools and receives no API key. Unknown vintages are labelled `unknown`;
standard size defaults to 750 ml unless the product says otherwise.

An independent conservative check requires meaningful wine-name tokens in
the product title and producer tokens in title/vendor. Descriptions cannot
make a neighboring cuvée pass this check. Uncertain aliases remain unknown;
coverage is less important than accepting the wrong wine.

Same-vintage matches take precedence, then nearest known vintage. Prefer
Danish DKK and standard bottles within that group. Deduplicate domains,
choose each domain's lowest supported matching price, then take up to three
shops' median. Notes retain n, spread, URLs, actual vintages, bottle sizes
and fixed conversions (EUR×7.46, SEK×0.66, NOK×0.64). They are static rough
rates, not live quotes. All prices normalize to 750 ml; `source` starts
`skøn:` and is <=100 characters. Never truncate provenance silently.

## Snapshot export and runner

`cellarion-price-snapshots` resolves exactly one running
`mongo-spuypiijq4qrcd0mzkfpm0qd-*` container with `docker ps`, then uses
`docker exec … mongosh --quiet --norc winecellar --eval …` for a full,
read-only `winevintageprices.find({})`. It uses the existing backup access
path, not JB's password or a new API credential. A complete successful scan
produces:

```
{as_of, complete: true, collection: "winevintageprices", count,
 prices: [{wine_id, vintage, price, currency, set_at}]}
```

Output is schema-checked and atomically replaced with mode 600, after the
entire query succeeds. On failure an old file remains intact and the command
exits nonzero. `ExecStartPre` runs this exporter before every monthly job;
its failure prevents the runner from starting and sends one Telegram line.
The runner also rejects incomplete or >24-hour-old input. No local journal
or requested-price queue is used to infer absence/freshness.

Apply targets active wine/vintages with no snapshot or a latest snapshot
older than 180 days, excluding all invoice wines. Dry runs also inspect
invoice wines, explicitly flagged, to validate protection and coverage.
Dry-run wine workers default to three (maximum four), sharing per-shop
limits. Writes are always serial, about one wine every two minutes.

The JSONL report contains candidates, model selection, provenance, per-shop
errors and search availability. Durable progress is written per wine. A
short monthly Telegram line reports priced / ukendt / >25% changes /
failures / search availability. Previous prices use the same fixed rates
for delta comparison, with USD×6.9 added for history; unconvertible history
is counted separately. Startup failure sends one failure line. There are
no automatic write retries: an ambiguous timeout could have committed.

The revised target is 60% coverage; lower coverage with honest `ukendt` is
accepted by JB. Report actual coverage and source exclusions without
weakening identity or source rules to reach a percentage.

## DevOps installation after merge and JB's ten-price check

Install `deploy/cellarion-cli` as `~/ops/bin/cellarion`, and install
`cellarion_pricebot.py`, executable `pricebot` and executable
`cellarion-price-snapshots` beside it. Install the unit files from
`deploy/systemd/` into `~/.config/systemd/user/`. SearXNG is optional;
authenticated `claude` subscription CLI and the existing OpenClaw Telegram
route must work as jb. Mongo read access must work as for cellarion-backup.

Perform the full dry run, give JB ten randomly selected accepted prices
with their URLs/bands/vintage caveats, and wait for his check before live
writes. Then test one price write and immediate undo. Finally:

```
systemctl --user daemon-reload
systemctl --user enable --now cellarion-pricebot.timer
systemctl --user list-timers cellarion-pricebot.timer
```

The timer runs monthly, day 1 around 04:15 Europe/Copenhagen. Logs:
`journalctl --user -u cellarion-pricebot.service`. Disable with
`systemctl --user disable --now cellarion-pricebot.timer`.
The intake protocol is in `deploy/INTAKE.md`.

Checks: `python3 -m unittest discover -s deploy -p 'test_pricebot.py'` and
`python3 -m unittest discover -s deploy -p 'test_compose.py'`.
