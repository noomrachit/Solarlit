# Moonlit Bot

A complete Discord moderation + utility bot with:

- **Moderation** — `/mod kick`, `/mod ban`, `/mod timeout`, `/mod warn`, `/mod warnings`, `/mod clear`
- **Welcome / leave** — `/welcome channel`, `/welcome message`, `/welcome leave` (placeholders: `{mention}` `{user}` `{name}` `{server}` `{count}`)
- **Automod** — `/automod toggle`, `anti_invite`, `anti_mention_spam`, `addword`, `removeword`, `listwords`
- **Custom commands** — `/customcommand add|remove|list|prefix` (default prefix `!`)
- **Reaction roles** — `/reactionrole add|remove`
- **Queue system** — `/queue join|leave|list|reset|panel` (with persistent buttons)
- **Support panel** — `/supportpanel panel` (2 buttons)
- **Logging** — `/settings logchannel`
- **Presence tracking** — records online/offline status to DB
- **Health endpoint** — `GET /health` for uptime monitors

## Local / Replit Setup

1. Create an application + bot at https://discord.com/developers/applications
2. Under **Bot**, enable:
   - ✅ Server Members Intent
   - ✅ Message Content Intent
   - ✅ Presence Intent
3. Under **OAuth2 → URL Generator**, select scopes `bot` + `applications.commands` and permissions: Manage Roles, Kick Members, Ban Members, Moderate Members, Manage Messages, Send Messages, Read Message History.
4. Copy the invite URL and invite the bot.
5. Copy `.env.example` → `.env` and fill in:
   ```
   DISCORD_BOT_TOKEN=...
   DATABASE_URL=postgresql://...
   ```
6. Install deps & run:
   ```bash
   pip install -r requirements.txt
   python moonlit_bot.py
   ```

## Deploy to Railway (24/7)

### 1. Push to GitHub
```bash
cd moonlit-bot
git init
git add .
git commit -m "Moonlit Bot complete"
# create repo on github.com then:
git remote add origin https://github.com/YOUR_USERNAME/moonlit-bot.git
git branch -M main
git push -u origin main
```

### 2. Create Railway project
1. Go to [railway.app](https://railway.app) → New Project → Deploy from GitHub repo
2. Select your moonlit-bot repo
3. Set **Root Directory** to `moonlit-bot` (if the repo root contains the folder)

### 3. Add PostgreSQL
- Click **New → Database → PostgreSQL**
- Railway will inject `DATABASE_URL` automatically

### 4. Set Variables
In the service **Variables** tab add:
| Variable | Value |
|---|---|
| `DISCORD_BOT_TOKEN` | your bot token |
| `PORT` | `8080` (optional, default 8080) |

### 5. Deploy
Railway deploys on every push. Check **Logs** — you should see:
```
Logged in as YourBot#1234
Synced XX commands
Health server running on port 8080
```

### Required Intents (reminder)
Discord Developer Portal → Bot → Privileged Gateway Intents:
- Server Members Intent
- Message Content Intent  
- Presence Intent

## Offline Alerts (optional)
Wire UptimeRobot / BetterUptime to `https://your-railway-domain.up.railway.app/health`
and post to a Discord webhook when down.

## Commands overview
Use `/help` inside Discord for the full list.
