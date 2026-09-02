# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Moonlit is a Discord bot suite (Thai-language commands/comments throughout) plus a web dashboard for
managing it. It is a pnpm workspace of TypeScript packages (API server, dashboard, command reference site,
shared DB/API libs) alongside several **standalone Python processes** (the main bot, a voice-tracking bot,
a voice-relay bot, a static marketing site) that are not part of the pnpm workspace and have no build step.

## Commands

There is no root `package.json` committed to this repo (CI and `replit.md` reference root-level
`pnpm run typecheck` / `pnpm run build`, but those scripts currently only exist by convention — verify a
root `package.json` exists before relying on them; if missing, run the per-package scripts below instead).

Per-package TypeScript commands (run from repo root with `--filter`, or `cd` into the package):

```bash
pnpm install                                        # install all workspace deps

pnpm --filter @workspace/api-server run dev          # build + start API server (port from $PORT)
pnpm --filter @workspace/api-server run typecheck

pnpm --filter @workspace/dashboard run dev           # Vite dev server, dashboard SPA
pnpm --filter @workspace/commands run dev            # Vite dev server, command-reference SPA
pnpm --filter @workspace/mockup-sandbox run dev

pnpm --filter @workspace/api-spec run codegen        # regenerate api-client-react + api-zod from openapi.yaml
pnpm --filter @workspace/db run push                 # drizzle-kit push (dev only)
pnpm --filter @workspace/db run push-force

pnpm --filter @workspace/scripts run hello           # tsx one-off scripts
```

No test runner (Jest/Vitest/pytest) is configured anywhere in the repo. CI (`.github/workflows/ci.yml`)
only runs `pnpm run typecheck`, `pnpm run build`, and, for the Python bot, `python -m py_compile
moonlit_bot.py database.py` (a syntax check, not real tests). There is no lint step in CI.

Python services each have their own venv and `requirements.txt` (`bot/`, `voicebot/`, `voicerelay/`,
`website/`). The Nix/Replit environment blocks system-wide `pip install`; always create a venv and pass
`--no-user`, per `.agents/memory/python-venv-nix.md`:

```bash
cd bot && python3 -m venv .venv && .venv/bin/pip install -q --no-user -r requirements.txt
.venv/bin/python moonlit_bot.py
```

Same pattern for `voicebot/voice_bot.py`, `voicerelay/relay_bot.py`, `website/server.py`.

## Architecture

### Workspace layout

- `artifacts/api-server` — Express 5 API (`@workspace/api-server`). Bundled with esbuild
  (`build.mjs`) into a single ESM file at `dist/index.mjs`; `pnpm run dev` builds then runs it.
- `artifacts/dashboard` — React/Vite admin dashboard (`@workspace/dashboard`), Discord-OAuth gated.
- `artifacts/commands` — React/Vite static command-reference site (`@workspace/commands`).
- `artifacts/mockup-sandbox` — standalone Vite playground for UI mockups, not wired to the API.
- `lib/db` — Drizzle ORM setup (`@workspace/db`), exports a `pg` `Pool` + `drizzle(pool, {schema})`.
- `lib/api-spec` — the OpenAPI spec (`openapi.yaml`) that is the single source of truth for the HTTP
  API surface, plus `orval.config.ts` which drives codegen.
- `lib/api-client-react` — generated (`src/generated/`) React Query hooks + a hand-written
  `custom-fetch.ts` fetch wrapper (used as the orval `mutator`), consumed by `dashboard`/`commands`.
- `lib/api-zod` — generated (`src/generated/`) Zod schemas/types for the same API surface, consumed by
  `api-server` route handlers for request/response types.
- `scripts` — misc `tsx` one-off scripts.
- `bot/`, `voicebot/`, `voicerelay/`, `website/` — independent Python processes (see below); **not** part
  of the pnpm workspace, no shared code with the TS side.
- `attached_assets/` — leftover uploads/snapshots from Replit (old zips, pasted snippets, a stale copy
  of the bot under `attached_assets/moonlit-bot/`); not live code, don't treat it as source of truth.
- `.agents/memory/` — durable notes from prior agent sessions (BigInt handling, auth flow, venv/Nix
  quirks). Read these before touching the areas they cover; add new ones for non-obvious discoveries.

### API contract flow (important: codegen is one-directional and manual)

`lib/api-spec/openapi.yaml` is edited by hand, then `pnpm --filter @workspace/api-spec run codegen` runs
orval twice against it (once for `api-client-react`, once for `api-zod`), writing into each package's
`src/generated/` and then runs `pnpm -w run typecheck:libs`. **The generated files are not regenerated
automatically** — after changing `openapi.yaml`, both `api-server` (consumes `@workspace/api-zod` types)
and `dashboard`/`commands` (consume `@workspace/api-client-react` hooks) will be out of sync until codegen
is re-run.

### Database: Drizzle schema is a stub — Postgres is actually owned by the Python bot

`lib/db/src/schema/index.ts` is an unpopulated template (no tables defined). The real schema and all
migrations live in `bot/database.py`'s `init_db()`: idempotent `CREATE TABLE IF NOT EXISTS` blocks plus
manual `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` migrations for anything added after initial table
creation (there is no migration tool/history — schema changes are additive SQL edits to `init_db()`).
Tables: `settings`, `warnings`, `custom_commands`, `reaction_roles`, `presence_logs`, `queue`,
`banned_words`, `queue_board`, `dashboard_board`, `queue_bookings`, `breakout_sessions`.

`artifacts/api-server` reads/writes these same tables with **raw SQL** via the `pool` exported from
`@workspace/db` (see `artifacts/api-server/src/routes/moonlit.ts`) — it does not use Drizzle's typed
query builder, because there's no Drizzle schema to build against. If you add a table or column, add it
to `bot/database.py`'s `init_db()` (the bot owns schema creation/migration), not to
`lib/db/src/schema/index.ts`, unless you're deliberately adopting Drizzle for that table.

Discord snowflake IDs are stored as `BIGINT` and exceed `Number.MAX_SAFE_INTEGER`. From Node, pass
`BigInt(id)` into queries; `pg` returns BIGINT columns as strings — convert explicitly with `String(...)`
when building response objects (`.agents/memory/bot-bigint-columns.md`).

### Auth flow (dashboard ↔ api-server)

Discord OAuth2, session-based (`express-session`, `SESSION_SECRET`):
1. `GET /api/auth/login` → redirects to Discord OAuth (scopes `identify guilds`).
2. `GET /api/auth/callback` → exchanges code, fetches user + guilds, filters to guilds where the user is
   owner or has `MANAGE_GUILD`, saves `{userId, username, globalName, avatar, accessToken, guilds[]}` into
   the session, redirects to `/`.
3. `requireAuth` middleware (`artifacts/api-server/src/middlewares/requireAuth.ts`) gates every route
   mounted after it in `routes/index.ts`; only `/api/healthz` and `/api/auth/*` are public.
4. Dashboard's `useAuth()` hook calls `/api/auth/me`; 401 renders `<Login />`, success renders the app.

The registered Discord OAuth redirect URI must exactly match what the server computes for
`getRedirectUri()` (in dev: `https://<REPLIT_DEV_DOMAIN>/api/auth/callback`), or the callback fails.

### Python services (independent of each other and of the TS workspace)

- `bot/moonlit_bot.py` — the main Discord bot. Single large file (~2k lines) organized as: helpers →
  event handlers (`on_ready`, `on_member_join`, `on_message`, etc.) → slash command groups
  (moderation `/mod *`, welcome, automod, custom commands `/cc *`, reaction roles `/rr *`, a
  booking/queue system, a support-ticket panel, per-guild `/settings *`, voice-channel management,
  breakout rooms). Talks to Postgres via `bot/database.py` (`asyncpg`, its own connection pool, not the
  Node `pg` pool). Runs a tiny `aiohttp` health-check server on `$BOT_HEALTH_PORT`.
- `voicebot/voice_bot.py` — separate Discord bot for voice-activity tracking/analytics (matplotlib
  charts). Has its own `database.py` and Discord bot token; unrelated to `bot/`.
- `voicerelay/relay_bot.py` (note: `voicerelay/relay bot.py`, with a space, also exists in the tree and
  is stale/duplicate — the underscore file is the one referenced by tooling) — a listener bot +
  N speaker bots relaying one voice channel's audio into several others in real time. Needs
  `LISTENER_BOT_TOKEN` and `SPEAKER_BOT_TOKEN_<n>` per concurrent target room; one-way audio only,
  ~0.3–0.8s latency by design (see the module docstring for the full constraint list).
- `website/` — a minimal `aiohttp` static server serving `index.html`/`docs.html`.

Each Python service reads its own `.env` (see root `.env.example` for the bot's variables:
`DISCORD_BOT_TOKEN`, `DISCORD_CLIENT_ID`, `DISCORD_CLIENT_SECRET`, `DATABASE_URL`, `SESSION_SECRET`,
`BOT_HEALTH_PORT`, `LOG_LEVEL`, `NODE_ENV`).

## Conventions

- TS packages are ESM (`"type": "module"`) with strict-ish `tsconfig.base.json` (`strictNullChecks`,
  `noImplicitAny`, etc., but `noUnusedLocals`/`strictFunctionTypes` off). Cross-package imports use
  workspace protocol (`"@workspace/db": "workspace:*"`).
- pnpm enforces a minimum npm package release age of 1440 minutes (1 day) as a supply-chain defense
  (`pnpm-workspace.yaml`) — new/just-published dependency versions will fail to install until they age
  out, unless added to `minimumReleaseAgeExclude`. Don't lower or remove this setting.
- `dashboard` and `commands` are near-duplicate Vite/React/Radix/Tailwind app shells (same dependency
  set) serving different audiences (admin UI vs. public command docs) — changes to shared UI patterns
  often need to land in both.
- Bot-facing code, comments, and command descriptions are predominantly in Thai; match that convention
  when touching `bot/`, `voicebot/`, `voicerelay/`, `website/`, and `SETUP.md`.

## Confirm before editing or saving files

Before writing to, editing, or saving any file in this repository, always ask the user for
explicit confirmation first — describe what will change and wait for approval before calling
Write/Edit. This applies on every session, every time, not just once.
