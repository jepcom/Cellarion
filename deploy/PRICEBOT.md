# Rough retail wine prices (DEV-76)

The CLI and runner use Cellarion data and MCP only; no application changes.
A stored number is an estimate per 750 ml, never an invoice or an AI guess.
Bands: <125 · 125–200 · 200–300 · 300–450 · 450–700 · 700–1000 ·
1000–1500 · 1500–2500 · >2500 DKK. At an exact boundary use the higher band.

## Commands

```
cellarion price-lookup WINE_ID --vintage 2020
cellarion set-price WINE_ID 2020 250 --source 'skøn: shop.dk 2026-10-20' --notes '…'
cellarion undo-last
pricebot --output report.jsonl
```

Lookup prints candidates and a pick; no remote write. It journals a read-only
entry (excluded from undo). Copy `source` and `somm_notes` from the pick into
`set-price --source ... --notes ...`; never substitute a guessed price.
`set-price` uses **only** `~/.config/cellarion/token-pricebot` (owner-only mode),
passes DKK explicitly, checks active bottles for invoice prices and journals
the new snapshot. It never changes `Bottle.price`. Any invoice on the wine
protects all its vintages, including a recorded zero. The server enforces
somm/admin and write scope. The current token is named **Claude** and also has
consume scope; pricebot's tool allowlist includes no consume tools. JB can
replace it with read+write without changing the installation.

`undo-last` selects the credential used by the last journalled write. Server
undo is account-wide and time limited; inspect the app before undoing when
another client may have written. Dry-run journal rows aren't undo units.
`undo-last --batch` changes credentials per journalled step back to bulk_add.

## Source and matching policy

Only reviewed retail domains in `cellarion_pricebot.SHOPS` can be fetched,
including redirects. Auction, blog, review and search paths are rejected.
News, review, marketplace, Vivino and Wine-searcher domains are absent from
the allowlist. Never add one to improve coverage. Robots rules are respected;
failed robots fetch, blocking and captchas are not bypassed. Product requests
are at least one second apart, at most ten pages per wine.

Accept one JSON-LD Product with explicit Offer price/currency, or
`product:price:amount` with `product:price:currency`. No text-snippet prices,
AggregateOffer low prices, category collections, unknown currencies or
out-of-stock offers. Haiku via the existing Claude subscription only matches
producer/cuvée, vintage, bottle count and size; it cannot supply a price or URL
and has no tools. Ambiguous matches stay `ukendt`. No API keys are supplied.

Prefer Danish DKK offers, then the same vintage, otherwise nearest vintage;
prefer standard 750 ml bottles. Deduplicate domains and take up to three
shops' median. Explicitly identified single bottles of other sizes are
normalized to 750 ml. Notes contain n, spread, URLs, matched vintages, sizes and
fixed conversions: EUR×7.46, SEK×0.66, NOK×0.64. Non-DKK offers are labelled
by their original currencies and rates in notes. These are rough static
rates, not current financial quotes. Source always starts `skøn:`.

## Runner and outstanding integration dependency

**Do not enable the timer until the price-history input is supplied.**
The current MCP `get_wine`/`get_bottle` responses expose no WineVintagePrice
history or timestamps. `list_price_tracking_requests` covers requested wines
only, and GET `/api/somm/prices/lookup` rejects personal API tokens under the
existing route allowlist. A local journal cannot prove absence or freshness
of snapshots written in the UI or by another client.

For now `--apply` therefore requires a fresh, complete `--snapshots FILE`
data export. Schema is in `pricebot --help`. Operations must provide that
export from existing data tooling and refresh it before each monthly run;
otherwise an authorized history read API is needed (outside this issue's
no-app-patch constraint). `complete: true` is an assertion by the export
producer, not something the runner can establish. Export age must be <=24h.
Never fabricate an empty export to bypass this gate.

With this input, target active wine/vintages with no snapshot or a latest
snapshot older than 180 days; skip invoice wines. One write attempt per two
minutes. Output JSONL is durable per wine, including failures. A monthly
Telegram summary reports priced / ukendt / >25% changes (only previous DKK
prices are compared) / failures. A startup failure sends one failure line;
per-wine failures are aggregated into the summary to avoid alert spam.
No automatic write retries: a timeout could have committed a snapshot.

## DevOps installation after merge and input integration

Install `deploy/cellarion-cli` as `~/ops/bin/cellarion`, plus
`cellarion_pricebot.py` and executable `pricebot` beside it. Install the two
systemd unit files into `~/.config/systemd/user/`. Keep state files private.
The existing SearXNG at 127.0.0.1:8888 and authenticated `claude` subscription
CLI must work as user jb. OpenClaw provides the existing Telegram route.
Run a full dry run and review the ten-price sample with JB before initial
bulk writes. Then test one `undo-last` immediately after a controlled price
write, before other clients write. Never delete bottles as an undo test.

After the authoritative export is automated and validated:

```
systemctl --user daemon-reload
systemctl --user enable --now cellarion-pricebot.timer
systemctl --user list-timers cellarion-pricebot.timer
```

Inspect with `journalctl --user -u cellarion-pricebot.service`. Disable with
`systemctl --user disable --now cellarion-pricebot.timer`.

Run local checks with `python3 -m unittest discover -s deploy -p 'test_pricebot.py'`.
