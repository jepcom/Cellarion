# Cellarion — migration into the Devshop (2026-10-19)

**Status: FROZEN. Do not change the running stack in `~/services/cellarion/`.**

All Cellarion work is now owned by the multica **Devshop** project **Cellarion**
(`a899c4a6-9586-4969-9646-61abcd3e9612`, workspace `3413ef11-0180-41c4-9bb3-0b8f65915bd5`).
Any agent that wants to touch Cellarion must pick up an issue in that project. The old
plan (`PLAN.md`) described a tailnet-only, hand-built deployment outside the devshop. JB
rejected that on 2026-10-19: it must live in the devshop so there is real access control.

## Why

- [decision] 2026-10-19 (JB): Cellarion must be a **devshop project**, deployed through the
  devshop-managed hosting (Coolify + Traefik on Theo) so access is managed, not
  `tailscale serve` on a port on Theo reachable only from JB's own devices.
- The earlier note "tailnet-only as chosen by JB" (Basic Memory `Wine cellar app comparison
  2026-10-19`, and `PLAN.md` phase 1) is **superseded**. It was Cleo's call, not the agreement.
- Timing is good: the registry is still effectively empty (one leftover test wine, no real
  bottles, no photos). Migrating **before** intake (old PLAN phase 3) is nearly free. After
  intake it means moving Mongo plus an uploads tree of label photos.

## Current state (verified 2026-10-19 19:05)

- Plain `docker compose`, project name `cellarion`, dir `~/services/cellarion/`
  (`docker-compose.yml`, `.env` 600, `data/uploads`, `PLAN.md`).
- Containers `cellarion-{mongo,meilisearch,backend,rembg,frontend}` on upstream release images
  `1.274.0`. Frontend published on `127.0.0.1:8095` only. Named volumes `cellarion_mongo-data`,
  `cellarion_meili-data`; uploads bind-mounted at `./data/uploads`.
- Exposure: `tailscale serve --https=8445 http://127.0.0.1:8095` →
  `https://theo.tail937cbc.ts.net:8445`, tailnet only. No Traefik route, no public DNS,
  not on the `coolify` network.
- Secrets in `~/services/cellarion/.env` (JWT_SECRET, MEILI_MASTER_KEY, SUPER_ADMIN_EMAIL,
  FRONTEND_URL/BACKEND_URL pointing at the tailnet URL, TRUST_PROXY_HOPS=2).
- Account `jesper` / jb@jepcom.dk exists with role `user`; the `admin` role still needs a Mongo
  write that JB must approve. Generated password in `~/.config/cellarion/jb.password` (600).
- Backups: `cellarion-backup.timer` 03:45 → `~/ops/bin/cellarion-backup` (mongodump via
  `docker exec cellarion-mongo` + tar of `~/services/cellarion/data/uploads`) →
  `~/backups/cellarion/`; off-site via the 04:30 restic run. Restore drill passed 2026-10-19
  (80/80 collection counts matched).
- Fork `jepcom/Cellarion` at `~/devel/cellarion`, branch `jb` = v1.274.0, no local changes.

## Target state

```
Browser / iPhone ──▶ https://<decided>.exerun.com (Traefik 3.6, Let's Encrypt)
                          │   ← access control: Cellarion's own accounts + roles
                          │     (registration closed, jesper = super admin)
                          │     optional Traefik basicauth in front (cognee-mcp pattern)
                          ▼
                     Coolify application "cellarion" (Docker Compose, git-sourced)
                     frontend ─▶ backend ─▶ mongo / meilisearch / rembg
                          ▲
HA Lundtofte / MCP ───────┘  (token auth, /api/mcp public endpoint off)
```

- **Devshop-managed**: a Coolify *Docker Compose* application, source = a git repo so every
  deployment change is a reviewable commit (Builder/DevOps Agent can PR it) instead of a file
  only present on Theo. Environment variables and secrets live in Coolify, not in a `.env`
  on disk.
- Compose source: branch `jb` of the fork `jepcom/Cellarion` carrying our `deploy/` compose, or
  a dedicated small repo `jepcom/cellarion-deploy`. Decide in DEV issue "compose source".
- Domain: `*.exerun.com` already wildcards to Theo (DNS at one.com), so no DNS work —
  only the hostname choice. Candidates: `vin.exerun.com`, `kaelder.exerun.com`,
  `cellarion.exerun.com`.
- Images stay pinned to upstream release tags; bumping a tag stays a deliberate commit.

## Decisions — answered by JB 2026-10-19 19:29 (gate DEV-58 closed)

1. **Hostname**: **`wines.exerun.com`**. Wildcard `*.exerun.com` already resolves to Theo, so no
   DNS work; Traefik issues the Let's Encrypt cert.
2. **Access control**: **option (a)** — public route through Traefik, Cellarion's own accounts and
   roles are the gate. Registration closed, `jesper` = super admin, `/api/mcp` public endpoint off
   and token-only. **No** Traefik basicauth in front (it would break the MCP token flow and the HA
   integration). Option (c) was not taken: the tailnet entry is not kept as a parallel path — it
   stays up only until the cutover is verified, then DEV-63 removes it.
3. **Compose source repo**: Cleo's call → **fork branch `jb` of `jepcom/Cellarion`, compose under
   `deploy/`**. App source and deployment then move on the same tag, and Builder/DevOps Agent
   review it as PRs on a repo that already exists.

Consequences now fixed for the implementation issues:

- `FRONTEND_URL` = `BACKEND_URL` = `https://wines.exerun.com`.
- Traefik → nginx → backend is two proxy hops, same as the tailnet setup, so `TRUST_PROXY_HOPS=2`
  stays correct. Verify with a request carrying `X-Forwarded-For` that the backend logs the real
  client IP, not Traefik's.
- Because the route is public from day one: registration must be closed *before* the first deploy
  is reachable, and rate limiting must not be loosened until intake actually needs it.

## Migration steps (ordered; each is a DEV issue)

1. **Gate** — JB answers the three decisions above. No agent proceeds past this.
2. **Compose for Coolify** — port `~/services/cellarion/docker-compose.yml` into the chosen repo:
   re-add Traefik/Coolify routing for the frontend, keep the hardening block (no-new-privileges,
   cap_drop, memory limits), keep mongo/meili internal (no published ports), uploads on a named
   volume instead of a bind mount. Review as a PR.
3. **Create the Coolify app** — new application, Docker Compose from that repo, FQDN set, env vars
   entered in Coolify (new `JWT_SECRET` and `MEILI_MASTER_KEY`; `FRONTEND_URL`/`BACKEND_URL` =
   the new https URL; `TRUST_PROXY_HOPS` corrected for Traefik → nginx → backend).
   Deploy **empty** and verify: login page over HTTPS with a valid cert, `/api/mcp`
   unauthenticated = 401, no mongo/meili port reachable from outside.
4. **Data move** — `docker compose stop` the old stack (no traffic), `mongodump --archive --gzip`
   from `cellarion-mongo`, `mongorestore` into the Coolify mongo, copy `data/uploads` into the new
   volume, compare all collection counts (same drill as 2026-10-19). Delete the leftover
   "Restore drill test" wine while we are in there.
5. **Re-point clients** — JB's bookmarks/iPhone; re-issue any `cel_` MCP token bound to the old
   URL; HA integration URL (phase 4 is not built yet, so nothing to change there today).
6. **Retarget backups** — `~/ops/bin/cellarion-backup` currently hardcodes `cellarion-mongo` and
   the bind-mount path. Switch to resolving the container by Coolify UUID prefix at run time (the
   `db-backup` pattern) and to `docker run --volumes-from`/`docker cp` for the uploads volume.
   Re-run the restore drill and confirm the 03:45 timer and the 04:30 off-site run still pass.
7. **Decommission the old path** — `tailscale serve --https=8445 off`, `docker compose down`
   (keep volumes until the drill in step 6 passed), archive `~/services/cellarion/` to
   `~/backups/cellarion/legacy-stack-2026-10-xx.tar.gz` (contains secrets, mode 600), and remove
   the stack dir. Keep this file as the record.
8. **Admin role for `jesper`** — still JB's call; the exact Mongo command is shown before it runs.
   Do it on the new stack, not the old one.
9. **Docs** — update the Basic Memory estate map and `Wine cellar app comparison 2026-10-19`
   (mark the tailnet decision superseded), and close this file out with the final shape.

Then, and only then, the old PLAN phases 3 (agent intake) and 4 (Home Assistant) run as their own
issues in the project.

## Rules for any agent reading this

- Do not start a step that is not an issue in the Devshop project **Cellarion**, assigned to you.
- Do not deploy, expose, or re-expose Cellarion on a new hostname or port without the gate (step 1)
  answered by JB.
- Do not grant the `admin` role, change passwords, or write to Mongo without JB's explicit go.
- `PLAN.md` in this directory is historical. Where it conflicts with this file, this file wins.

## Decommission record (DEV-63, 2026-10-19)

The legacy tailnet-only stack is gone; the Coolify application `cellarion`
(uuid `spuypiijq4qrcd0mzkfpm0qd`) at `https://wines.exerun.com` is the only Cellarion.

- Precondition: DEV-62 restore drill green (80/80 collections, `siteconfigs` incl. `rateLimits`).
- `tailscale serve --https=8445 off` — `https://theo.tail937cbc.ts.net:8445` no longer served.
- `docker compose down` in `~/services/cellarion/` (containers + network removed).
- Archives in `~/backups/cellarion/` (mode 600, contain secrets):
  - `legacy-stack-2026-10-19.tar.gz` — the whole stack dir incl. `.env`, compose, PLAN.md, this file.
  - `legacy-volume-mongo-data-2026-10-19.tar.gz`, `legacy-volume-meili-data-2026-10-19.tar.gz` —
    raw contents of the old named volumes, taken after the stack was stopped.
  - `legacy-stack-2026-10-19-1634/` — last mongodump of the legacy stack (from DEV-62).
- Then removed `~/services/cellarion/` and the volumes `cellarion_mongo-data`, `cellarion_meili-data`.
- This file in the repo (`deploy/MIGRATION.md`) is now the record; the on-host copy no longer exists.
