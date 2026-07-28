---
name: Dashboard auth flow
description: How Discord OAuth + session auth works across the API server and dashboard
---

**Architecture:**
- `express-session` with `SESSION_SECRET` env var on the API server
- Public routes: `/api/healthz`, `/api/auth/login`, `/api/auth/callback`, `/api/auth/me`, `/api/auth/logout`
- Protected: all other `/api/*` routes via `requireAuth` middleware in `routes/index.ts`
- Dashboard: `useAuth()` hook calls `/api/auth/me`; 401 → renders `<Login />` page; loading → spinner; success → full app

**OAuth flow:**
1. `/api/auth/login` → redirect to Discord OAuth (scopes: `identify guilds`)
2. `/api/auth/callback` → exchange code, fetch user + guilds, filter to admin-only guilds (owner || MANAGE_GUILD permission), save session, redirect to `/`
3. Session stores: userId, username, globalName, avatar, accessToken, guilds[]

**Required env vars:** `SESSION_SECRET`, `DISCORD_CLIENT_ID`, `DISCORD_CLIENT_SECRET`
**Optional:** `DISCORD_REDIRECT_URI` (defaults to `https://${REPLIT_DEV_DOMAIN}/api/auth/callback`)

**Important:** The redirect URI registered in the Discord Developer Portal must exactly match what `getRedirectUri()` returns. In dev this is `https://<REPLIT_DEV_DOMAIN>/api/auth/callback`.
