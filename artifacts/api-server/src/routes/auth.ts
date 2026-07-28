import { Router, type IRouter } from "express";
import { logger } from "../lib/logger";
import type { DiscordGuild } from "../types/session";

const router: IRouter = Router();

const DISCORD_API = "https://discord.com/api/v10";
const SCOPES = "identify guilds";
const MANAGE_GUILD = 0x20n;

function getClientId(): string {
  const id = process.env["DISCORD_CLIENT_ID"];
  if (!id) throw new Error("DISCORD_CLIENT_ID is not set");
  return id;
}

function getClientSecret(): string {
  const secret = process.env["DISCORD_CLIENT_SECRET"];
  if (!secret) throw new Error("DISCORD_CLIENT_SECRET is not set");
  return secret;
}

function getRedirectUri(): string {
  const explicit = process.env["DISCORD_REDIRECT_URI"];
  if (explicit) return explicit;
  const domain = process.env["REPLIT_DEV_DOMAIN"];
  if (domain) return `https://${domain}/api/auth/callback`;
  throw new Error(
    "Set DISCORD_REDIRECT_URI or ensure REPLIT_DEV_DOMAIN is available",
  );
}

// GET /api/auth/login — redirect to Discord OAuth
router.get("/auth/login", (_req, res): void => {
  const params = new URLSearchParams({
    client_id: getClientId(),
    redirect_uri: getRedirectUri(),
    response_type: "code",
    scope: SCOPES,
  });
  res.redirect(`https://discord.com/api/oauth2/authorize?${params}`);
});

// GET /api/auth/callback — Discord redirects here with ?code=
router.get("/auth/callback", async (req, res): Promise<void> => {
  const code = req.query["code"];
  if (typeof code !== "string") {
    res.status(400).send("Missing code parameter");
    return;
  }

  try {
    // Exchange code for token
    const tokenRes = await fetch(`${DISCORD_API}/oauth2/token`, {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body: new URLSearchParams({
        client_id: getClientId(),
        client_secret: getClientSecret(),
        grant_type: "authorization_code",
        code,
        redirect_uri: getRedirectUri(),
      }),
    });

    if (!tokenRes.ok) {
      const err = await tokenRes.text();
      logger.error({ err }, "Discord token exchange failed");
      res.status(500).send("OAuth token exchange failed");
      return;
    }

    const tokenData = (await tokenRes.json()) as {
      access_token: string;
      token_type: string;
    };

    const accessToken = tokenData.access_token;

    // Fetch user + guilds in parallel
    const [userRes, guildsRes] = await Promise.all([
      fetch(`${DISCORD_API}/users/@me`, {
        headers: { Authorization: `Bearer ${accessToken}` },
      }),
      fetch(`${DISCORD_API}/users/@me/guilds`, {
        headers: { Authorization: `Bearer ${accessToken}` },
      }),
    ]);

    if (!userRes.ok || !guildsRes.ok) {
      logger.error("Failed to fetch Discord user or guilds");
      res.status(500).send("Failed to fetch Discord profile");
      return;
    }

    const user = (await userRes.json()) as {
      id: string;
      username: string;
      global_name?: string | null;
      avatar?: string | null;
    };
    const allGuilds = (await guildsRes.json()) as DiscordGuild[];

    // Only keep guilds where the user is owner or has Manage Guild permission
    const adminGuilds = allGuilds.filter(
      (g) => g.owner || (BigInt(g.permissions) & MANAGE_GUILD) !== 0n,
    );

    req.session.userId = user.id;
    req.session.username = user.username;
    req.session.globalName = user.global_name ?? null;
    req.session.avatar = user.avatar ?? null;
    req.session.accessToken = accessToken;
    req.session.guilds = adminGuilds;

    req.session.save((err) => {
      if (err) {
        logger.error({ err }, "Session save failed");
        res.status(500).send("Session error");
        return;
      }
      // Redirect to the dashboard root
      const dashboardUrl = process.env["REPLIT_DEV_DOMAIN"]
        ? `https://${process.env["REPLIT_DEV_DOMAIN"]}/`
        : "/";
      res.redirect(dashboardUrl);
    });
  } catch (err) {
    logger.error({ err }, "OAuth callback error");
    res.status(500).send("Internal error during OAuth");
  }
});

// GET /api/auth/me — return current session user
router.get("/auth/me", (req, res): void => {
  if (!req.session?.userId) {
    res.status(401).json({ error: "Unauthorized" });
    return;
  }
  res.json({
    userId: req.session.userId,
    username: req.session.username,
    globalName: req.session.globalName,
    avatar: req.session.avatar,
    guilds: req.session.guilds ?? [],
  });
});

// POST /api/auth/logout — destroy session
router.post("/auth/logout", (req, res): void => {
  req.session.destroy((err) => {
    if (err) {
      logger.error({ err }, "Session destroy failed");
      res.status(500).json({ error: "Logout failed" });
      return;
    }
    res.json({ ok: true });
  });
});

export default router;
