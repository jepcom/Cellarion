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
9. **Report back** in one message: what was added (count per wine), the photo, the placements, and
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
