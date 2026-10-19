# Cellarion on Coolify

DEV-59 supplies the source only. DEV-60 owns application creation and deployment;
DEV-61 owns the data move, DEV-62 backups, DEV-63 the old route, and DEV-64 roles.
Do not run this Compose file against the legacy stack.

## Application and routing (DEV-60)

Use the Git-sourced **Docker Compose application** build pack, repository
`jepcom/Cellarion`, branch `jb`, base directory `/`, Compose location
`/deploy/docker-compose.yml`. Do not use the root upstream Compose/override files.

Keep Coolify's managed proxy labels enabled. Set **only frontend's** domain to
`https://wines.exerun.com:8080`. The suffix selects nginx's internal port; the
browser URL is `https://wines.exerun.com` on normal HTTPS port 443. Coolify generates
Traefik routing, HTTP-to-HTTPS redirect and certificate configuration, and connects
its proxy to the resource network. No external `web` network or host port mapping
is needed. Do not add domains to backend, mongo, meilisearch or rembg; their
`traefik.enable=false` labels explicitly opt them out of routing. Do not enable
Traefik basicauth: personal MCP tokens and Home Assistant must reach the API.

See [Coolify Compose application documentation](https://coolify.io/docs/applications/builds/docker-compose)
and [domain port syntax](https://coolify.io/docs/core/networking/domains).

Fixed container names and the legacy project name are intentionally omitted so
Coolify can isolate this application while the frozen stack remains running.
Service DNS names stay `backend`, `mongo`, `meilisearch`, `rembg`, `frontend`.
The Compose volumes are `mongo-data`, `meili-data`, and **`uploads-data`** (mounted
at `/app/uploads`). Let Coolify prefix them with its application identifier; do
not bind them to the legacy volumes. DEV-61/62 must resolve the actual managed
volume names from the rendered application instead of assuming `cellarion_*`.

## Coolify environment

There is no repository `.env` or `env_file`. Enter these six runtime variables in
Coolify; both secrets must be freshly generated at deployment time and kept there.

| Variable | Value |
| --- | --- |
| `JWT_SECRET` | Fresh strong random secret; required, no default |
| `MEILI_MASTER_KEY` | Fresh strong random secret; required, no default; shared by backend and Meilisearch |
| `FRONTEND_URL` | `https://wines.exerun.com` |
| `BACKEND_URL` | `https://wines.exerun.com` |
| `SUPER_ADMIN_EMAIL` | `jb@jepcom.dk` |
| `TRUST_PROXY_HOPS` | `2` |

The last four also have these defaults in Compose. Fixed container variables are:
backend `NODE_ENV=production`, `NODE_OPTIONS=--max-old-space-size=480`,
`MONGO_URI=mongodb://mongo:27017/winecellar`, `PORT=5000`,
`MEILI_URL=http://meilisearch:7700`, `REMBG_URL=http://rembg:5000`;
Meilisearch `MEILI_ENV=production`, `MEILI_MAX_INDEXING_MEMORY=128Mb`,
`MEILI_MAX_INDEXING_THREADS=2`, `MEILI_NO_ANALYTICS=true`.
No other variables need entering. Optional upstream integrations are not forwarded.

Mongo retains the upstream digest; Meilisearch retains upstream's `v1.45.1` tag
(upstream does not digest-pin it). All three Cellarion images stay at `1.274.0`.
The legacy hardening anchor, capability exception, memory limits and healthchecks
are preserved.

## Before making the route public

The `jesper` super-admin role and disabling anonymous `/api/mcp/public` require
application configuration/data; `SUPER_ADMIN_EMAIL` alone does not complete
these prerequisites. In v1.274.0, `backend/src/routes/auth.js` registers
`POST /register` without a registration-closure switch. DEV-60 must resolve
registration closure before public exposure and coordinate the role prerequisite
with DEV-64/JB. This PR does not implement application changes or authorize
database writes. The anonymous MCP switch is `mcp.publicEnabled=0` in the
admin rate-limit configuration; preserve personal MCP with `mcp.enabled=1`.
Keep personal `/api/mcp` token access available and verify unauthenticated access
returns 401. Verify valid HTTPS, no host-published service ports, and client IP
resolution with a real `X-Forwarded-For` request through Traefik → nginx → backend.
These are deployment checks, not claimed as verified by the source-only tests.

`MIGRATION.md` is an unchanged copy of the frozen migration record. Its final
“Decisions” section supersedes the earlier candidate hostnames and optional auth.

## Source-only validation

Run `python3 deploy/test_compose.py`. It uses Docker Compose's config renderer
with public dummy values and an empty environment file; it starts no containers
and does not read deployment secrets. Deployment smoke checks remain with DEV-60.
