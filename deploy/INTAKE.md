# Cellar intake from a shelf photo

How any agent (Cleo on Telegram, a Devshop agent, Claude) adds bottles to JB's Cellarion at
https://wines.exerun.com from a photo. Follow this exactly so every intake works the same way.

## Tooling

- CLI: `~/ops/bin/cellarion` on Theo (source copy: `deploy/cellarion-cli` in this repo). It talks
  to Cellarion's MCP endpoint and prints JSON. Run `cellarion --help` for the verbs.
- Token: personal API token `cellarion-cli (theo ~/ops/bin/cellarion, DEV-70)`, scopes
  read + write + consume, in `~/.config/cellarion/token` (mode 600). The CLI refuses to run if the
  file is missing or readable by others. Never print, paste, or store the token anywhere else.
  To rotate it, JB revokes it in Cellarion (Settings → API tokens), creates a new one with the same
  scopes, and writes it to that file with mode 600.
- Local journal: `~/.local/state/cellarion/journal.jsonl` records what the CLI wrote, so
  `undo-last --batch` can take back a whole intake.

## Protocol

1. **Find the target.** `cellarion list-cellars` → cellar id. `cellarion racks <cellar_id>` →
   rack ids, layout and free slots. If JB has no cellar or rack for this shelf yet, ask him what to
   call it. Don't invent a layout. (`cellarion create-cellar <name>` exists for the first cellar.)
2. **Read the labels.** For each distinct bottle in the photo, read producer, wine name, vintage,
   region/appellation, type and quantity (count identical bottles). If a field is unreadable, say so.
   Never guess a producer.
3. **Resolve each wine** against the registry:
   `cellarion resolve --name <wine> --producer <producer> --country <country> --type <type> --vintage <v>`.
   If it matches, use its `wine_id`. If there's no match, use `new_wine` (name WITHOUT the producer,
   producer, country, type, plus region/appellation/grapes if known). If there are close candidates,
   pick one or ask JB.
4. **Propose a table to JB.** One row per wine: producer, wine, vintage, quantity, rack + position,
   drink window (`drink_from`–`drink_to`), and a **one-line reason** for the window (e.g. "Musar
   needs 10+ years; 2015 opens ~2025, holds to 2040"). The drink window is **required**: the Home
   Assistant cellar sensors (ready to drink, past peak) stay at zero without it.
5. **Write nothing until JB confirms.** Apply corrections he gives and re-show the table if anything
   changed.
6. **Preview, then apply, one batch at a time.** A batch is at most **24 bottles** (the server
   limit). Each batch is one undo unit. Write the items as JSON:
   ```json
   [{"wine_id": "…", "vintage": "2015", "quantity": 6, "drink_from": 2025, "drink_to": 2040},
    {"new_wine": {"producer": "…", "name": "…", "country": "…", "type": "red"},
     "vintage": "2019", "quantity": 2, "drink_from": 2024, "drink_to": 2032,
     "price": 250, "currency": "DKK"}]
   ```
   `cellarion add --cellar <cellar_id> items.json` returns a plan and a `preview_id`, with nothing
   written yet. Check that the plan matches the confirmed table: no `blocked_similar` or `invalid` rows,
   and `will_create` only for wines you meant to create. Then run `cellarion add --apply <preview_id>`
   (the preview stays valid for 15 minutes). The response lists `added_bottle_ids`.
   If you give a price, always pass `currency` too. Without it the server defaults to USD.
7. **Attach the photo** once per wine:
   `cellarion photo <file> --wine <wine_id>` (or `--bottle <id>`). Use `--label-only` only for a
   photo of the label alone filling the frame. A photo of the whole shelf is not a label photo.
   Attach the shelf photo to one wine, or crop per bottle if you can.
8. **Place the bottles**: `cellarion place <rack_id> <position> <bottle_id>` for each bottle,
   using the positions JB confirmed. The response reports any bottle displaced from the slot.
   If something was displaced, tell JB.
9. **Price newly added wines** after `add --apply`: resolve the registry wine ID
   from each new bottle with MCP `get_bottle`, then run
   `cellarion price-lookup <wine_id> --vintage <v>`. If a supported shop or labelled comparable pick
   exists, run `cellarion set-price <wine_id> <v> <price_dkk> --source <pick.source>
   --notes <pick.somm_notes>`. This uses only `token-pricebot`, never Cleo's token.
   Invoice wines are skipped. Lookups use the cached catalogue with no shop requests or crawl; missing supported comparables means `ukendt`. Preserve `estimat:` for comparable estimates and `skøn: shops` for shop matches. Report lookup
   failure without failing or repeating the completed intake. See [PRICEBOT.md](PRICEBOT.md).
10. **Report back** in one message: what was added (count per wine), the photo, the placements, and
   any problems.

## Undo

- `cellarion undo-last` undoes the newest MCP change on JB's account (one step).
- `cellarion undo-last --batch` undoes everything the CLI did since the last `add --apply`:
  placements and photos newest-first, then the whole batch (bottles plus any registry wines the batch
  created). It checks each step reverted what the journal expected. If another client's change came
  in between, it stops and says so. If that happens, check in the app before going further.
- Undo only works while the bottles are untouched (not consumed or edited since), and only within
  the server's undo window. Take back a bad batch right away.

## Don'ts

- No bulk import before JB is actually photographing and confirming.
- No writes without JB's confirmation of the table.
- Never put the token, or JB's password (`~/.config/cellarion/jb.password`), in chat, logs, issues
  or memory.

## Bulk import from a file (no AI key needed)

For an old spreadsheet, a Vivino export or a CellarTracker export, use the app, not the CLI:
**Cellar → Import bottles** at https://wines.exerun.com. This server has **no AI key** (no
`ANTHROPIC_API_KEY`, `OPENAI_*` or `VOYAGE_API_KEY`), and the file import still works. Checked in
DEV-72 against v1.274.0 (`frontend/src/utils/importMappers.js`, `backend/src/routes/import.js`) and
with a live run.

### What works without AI

| Path | Works without a key? | Notes |
|---|---|---|
| Plain CSV (your own spreadsheet) | Yes | Parsed in the browser, then `POST /api/bottles/import/validate` → review → `/confirm` |
| Vivino CSV export | Yes | Same flow |
| CellarTracker CSV export | Yes | Same flow; also places bottles in racks from `Location` + `Bin` |
| Cellarion export (`.json`/`.zip`, Settings → import) | Yes | `POST /api/cellar-import`; no AI at all |
| Receipt photo/PDF | **No** | `POST /api/bottles/import/receipt` returns 503 `ai_unavailable`. There is no OCR fallback. |

The CSV is parsed in the browser. The server only receives the mapped rows, so the column names
below are what matters. Header matching ignores case, spaces, `_`, `-` and `.`. Save the file as
UTF-8 CSV; comma or semicolon both work.

### How a row is resolved without AI

The registry starts empty here. There is nothing to match against yet, so in practice each
row ends up in one of these outcomes:

- **Created automatically (`ai_new`, shown as "new wine")**: the row has **Producer + Wine +
  a recognised Country + Type** (red/white/rosé/sparkling/dessert/fortified). The wine is created
  from the file at confirm, with no AI call and no cost. A producer prefix in the wine name is
  stripped ("Château Musar Rouge" with producer "Château Musar" becomes wine "Rouge").
- **Exact/fuzzy match**: the wine is already in the registry (for example from an earlier
  import). It is pre-selected.
- **No match**: any row missing one of those four fields. You confirm it by hand on the review
  screen. "Request wine" imports the bottle now and puts the wine in the admin queue (Admin → Wine
  requests), where JB, as admin, creates the wine. The other options are picking a wine via
  search, or skip.
- **Refused**: a row with neither a wine name nor a producer (`error: Wine name or producer is
  required`). The browser also drops rows with no wine name (`no-identity-skipped` warning).

So the one rule for a cheap import: **fill Producer, Wine, Country and Type on every row.**

Without AI, a producer written only inside the wine name is not split out for a plain CSV
("Domaine Leflaive Puligny-Montrachet" with an empty Producer stays no-match). The backend can
only split off producers that are already in the registry. The CellarTracker mapper does guess
producers that start with Château/Domaine/Clos/Bodega/Weingut/Quinta/… . Put the producer in its
own column.

### Plain CSV: recommended template

```csv
Producer,Wine,Vintage,Country,Region,Appellation,Type,Quantity,Size,Price,Currency,Purchase Date,Store,Location,Notes,Rating
Château Musar,Rouge,2016,Lebanon,Bekaa Valley,,Red,6,750ml,320,DKK,2024-03-01,Vinspecialisten,Kælder A,,
```

Columns the plain-CSV mapper reads (first alias that exists wins):

| Field | Column names |
|---|---|
| Wine (required) | `Wine`, `Wine Name`, `WineName`, `Name` |
| Producer | `Producer`, `Winery`, `Maker` |
| Vintage | `Vintage`, `Year` (empty → NV, with a warning) |
| Country / Region / Appellation | `Country`; `Region`; `Appellation`, `Sub-Region`, `SubRegion` |
| Classification | `Classification` |
| Type | `Type`, `Color`, `Colour`, `Category` (text containing red/white/rosé/sparkling/champagne/dessert/port/…) |
| Quantity | `Quantity`, `Qty`, `Count` (one bottle per unit is created) |
| Size | `Size`, `Bottle Size`, `BottleSize` (default 750ml) |
| Price | `Price`, `Price per Bottle`, `Bottle Price`, `Unit Price`, `Purchase Price`, `Price Paid`, `Cost`, `Pris`, `Preis`, `Prix`, … or a header naming the currency, e.g. `Price (DKK)` |
| Currency | `Currency` (**set it**; a price without a currency is not DKK) |
| Purchase | `Purchase Date`, `PurchaseDate`, `Date`; `Store`, `Purchase Location`, `Vendor` |
| Location | `Location`, `Bin` (free text) |
| Racks (optional) | `Rack` + `Rack Position`/`Position`/`Slot`, or `Rack`+`Row`+`Col`, or combined `Rack Location` like `M2-11`; `Rack Rows`/`Rack Cols`/`Rack Type` size new racks |
| Notes | `Notes`, `Note`, `Comments` |
| Rating | `Rating`, `Score` (scale guessed: ≤5, ≤20 or 100-point) |

There is **no grapes column** in the plain-CSV or Vivino mapper, so `Type` must be filled in. If
the headers are exactly camelCase `wineName,producer,vintage,…`, the file is read as a Cellarion
CSV export instead. Same fields, camelCase names.

### Vivino export

Upload Vivino's `full_wine_list.csv` (or `cellar.csv`) as is. Detected by a `Wine name` or `Winery`
header. Read: `Winery` → producer, `Wine name`, `Vintage`, `Country`, `Region`, `Appellation`,
`Wine type`, `Quantity`, `Bottle size`, `Price`/`Purchase Price` + `Currency`, `Purchase date`,
`Store name`, `Your rating`, `Your review`, `Personal Note`, `Drinking Window`, `Location`/`Bin`.
`Average rating` is ignored. A scan-history file (has `Scan date`) can go to the wishlist instead.
Rows with Winery + Wine name + Country + Wine type are created automatically. Others need a
request or a pick.

### CellarTracker export

Export **all** rows (not "only this page": exactly 25 rows triggers a truncation warning) as CSV.
Detected by `iWine`/`Barcode`, or by `Wine` + `Vintage` + `Locale`/`Bin`. Read: `Wine`, `Producer`,
`Vintage` (1001 → NV), `Quantity`/`Pending`, `Location` + `Bin` (→ racks), `Price`/`NativePrice`
+ currency, `Size`, `Type`/`Color`/`Category`, `Country`/`Region`/`SubRegion`/`Appellation` or
`Locale`, `Varietal`/`MasterVarietal` (grapes), `BeginConsume`/`EndConsume`, `MY`/`MyScore`, notes.

### Features that need a paid key (not enabled; that is DEV-74)

- **Label scan from the phone camera** (`POST /api/wines/scan-label`): Anthropic, default model
  `claude-sonnet-5` (at $2/$10 per M tokens, roughly $0.01–0.02 per label).
- **Receipt import**: same vision model, roughly $0.02–0.05 per receipt.
- **AI identification of incomplete import rows**: per unique wine, roughly $0.01 each. It is
  capped by the AI budget.
- **Cellar Chat**: needs both an Anthropic key (default `claude-haiku-4-5`, $1/$5 per M tokens,
  about $0.01 per question) **and** an embeddings key (`VOYAGE_API_KEY`, `voyage-4-large`). Embedding
  a cellar of a few hundred wines costs cents.

## Reading the cellar for food pairing (DEV-73)

`cellarion cellar [--cellar ID] [--all]` returns the drinkable cellar in one call: wines in stock grouped
by wine+vintage with quantity, rack slot (`Kælder / Reol A #7 (row 2, col 3)`), `drink_window` and
`window` = `in | approaching | past | unknown` (`--all` adds `not_yet` and reserved bottles).
`counts.in_stock` = 0 means the cellar is empty. Pagination is handled inside the command.

Chef and Ray have no shell, so they get it as the MCP tool `get_cellar` from `deploy/cellar-read-mcp`
(installed as `~/ops/bin/cellar-read-mcp`, registered as `mcp.servers.cellar` in `~/.openclaw/openclaw.json`).
It always uses `~/.config/cellarion/token-read`, a personal API token with scope `read` only — Cellarion
does not even list write tools for it. Pairing rules for the agents live in Basic Memory:
`projects/wine-cellar/Wine pairing from the cellar - get_cellar`.
