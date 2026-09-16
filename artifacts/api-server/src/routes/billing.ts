import { Router, type IRouter } from "express";
import { logger } from "../lib/logger";
import { billingPool, isBillingDbConfigured } from "../db/billingPool";
import { requireAuth } from "../middlewares/requireAuth";
import {
  createOmiseCustomer,
  chargeCustomer,
  getOmisePublicKey,
  isOmiseConfigured,
} from "../lib/omise";

const router: IRouter = Router();

// ─── Plan catalog ───────────────────────────────────────────────────────────
// Mirrors website/index.html's pricing section exactly — keep both in sync.
// relayBotLimit counts the listener bot ("หัวหน้า") + speaker bots
// ("ลูกน้อง") together, same as voicerelay/relay_bot.py's `limit` check.
export const PLANS = {
  standard: { name: "STANDARD", priceThb: 120, relayBotLimit: 6 },
  pro: { name: "PRO", priceThb: 250, relayBotLimit: 12 },
} as const;
export type PlanTier = keyof typeof PLANS;
const TRIAL_RELAY_BOT_LIMIT = PLANS.pro.relayBotLimit; // full access during trial

// เซิร์ฟเวอร์ทดลอง/companion ที่ยกเว้นการเก็บเงินทุกกรณี — mirrors
// voicerelay/access.py's EXEMPT_GUILD_IDS (ต้องแก้พร้อมกันทั้งสองที่)
const EXEMPT_GUILD_IDS = new Set<string>([
  "1359530731872718858", // COMPANION - SOLARLIT
  "1420296466718658613",
]);

function isPlanTier(v: unknown): v is PlanTier {
  return typeof v === "string" && v in PLANS;
}

function guildIdFromParam(req: { params: Record<string, unknown> }): string {
  const raw = req.params["guildId"];
  return Array.isArray(raw) ? raw[0] : (raw as string);
}

/** True if the logged-in user has Manage Guild / owner on this guild (same admin-guild list auth.ts computed at login). */
function userOwnsGuild(
  guilds: { id: string }[] | undefined,
  guildId: string,
): boolean {
  return Boolean(guilds?.some((g) => g.id === guildId));
}

// ─── Public: plan list + whether payments are live ─────────────────────────
router.get("/billing/config", (_req, res): void => {
  res.json({
    omiseConfigured: isOmiseConfigured(),
    omisePublicKey: getOmisePublicKey(),
    plans: PLANS,
  });
});

// ─── Protected: current subscription status for one guild ─────────────────
router.get("/billing/:guildId/status", requireAuth, async (req, res): Promise<void> => {
  const guildId = guildIdFromParam(req);
  if (!userOwnsGuild(req.session.guilds, guildId)) {
    res.status(403).json({ error: "Not an admin of this guild" });
    return;
  }

  if (EXEMPT_GUILD_IDS.has(guildId)) {
    res.json({
      tier: "exempt",
      status: "active",
      trialEndsAt: null,
      currentPeriodEnd: null,
      relayBotLimit: 99,
    });
    return;
  }

  if (!billingPool) {
    res.status(503).json({ error: "Billing database is not configured" });
    return;
  }

  const result = await billingPool.query(
    `SELECT tier, status, trial_ends_at, current_period_end
     FROM guild_subscriptions WHERE guild_id = $1`,
    [BigInt(guildId)],
  );

  if (!result.rows[0]) {
    // No row yet = brand new guild, not yet tracked. Report as a fresh
    // 30-day trial (matches website copy) without writing anything —
    // the row is created lazily on first /relay use or first checkout.
    res.json({
      tier: "trial",
      status: "trialing",
      trialEndsAt: null,
      currentPeriodEnd: null,
      relayBotLimit: TRIAL_RELAY_BOT_LIMIT,
    });
    return;
  }

  const row = result.rows[0];
  const limit =
    row.status === "active" && isPlanTier(row.tier)
      ? PLANS[row.tier as PlanTier].relayBotLimit
      : TRIAL_RELAY_BOT_LIMIT;

  res.json({
    tier: row.tier,
    status: row.status,
    trialEndsAt: row.trial_ends_at,
    currentPeriodEnd: row.current_period_end,
    relayBotLimit: limit,
  });
});

// ─── Protected: subscribe / change plan ────────────────────────────────────
// Body: { tier: "standard" | "pro", omiseToken: string }
// omiseToken comes from Omise.js (card popup) running client-side in the
// dashboard — the raw card number never touches this server (PCI scope).
router.post("/billing/:guildId/subscribe", requireAuth, async (req, res): Promise<void> => {
  const guildId = guildIdFromParam(req);
  if (!userOwnsGuild(req.session.guilds, guildId)) {
    res.status(403).json({ error: "Not an admin of this guild" });
    return;
  }
  if (EXEMPT_GUILD_IDS.has(guildId)) {
    res.status(400).json({ error: "This guild is exempt from billing — no subscription needed" });
    return;
  }
  if (!billingPool) {
    res.status(503).json({ error: "Billing database is not configured" });
    return;
  }
  if (!isOmiseConfigured()) {
    res.status(503).json({ error: "Payments are not configured yet" });
    return;
  }

  const { tier, omiseToken } = req.body as {
    tier?: unknown;
    omiseToken?: unknown;
  };
  if (!isPlanTier(tier)) {
    res.status(400).json({ error: "Invalid tier" });
    return;
  }
  if (typeof omiseToken !== "string" || !omiseToken) {
    res.status(400).json({ error: "Missing omiseToken" });
    return;
  }

  const plan = PLANS[tier];

  try {
    const customer = await createOmiseCustomer(omiseToken);
    const charge = await chargeCustomer(
      customer.id,
      plan.priceThb * 100,
      `Solarlit ${plan.name} — guild ${guildId}`,
    );

    if (charge.status !== "successful") {
      res.status(402).json({ error: "Charge not successful", status: charge.status });
      return;
    }

    await billingPool.query(
      `INSERT INTO guild_subscriptions
         (guild_id, tier, status, current_period_end, omise_customer_id, omise_charge_id, created_by_user_id, updated_at)
       VALUES ($1, $2, 'active', now() + interval '30 days', $3, $4, $5, now())
       ON CONFLICT (guild_id) DO UPDATE SET
         tier = EXCLUDED.tier,
         status = 'active',
         current_period_end = EXCLUDED.current_period_end,
         omise_customer_id = EXCLUDED.omise_customer_id,
         omise_charge_id = EXCLUDED.omise_charge_id,
         updated_at = now()`,
      [BigInt(guildId), tier, customer.id, charge.id, req.session.userId],
    );

    res.json({ ok: true, tier, chargeId: charge.id });
  } catch (err) {
    logger.error({ err, guildId, tier }, "Subscribe failed");
    res.status(502).json({ error: "Payment provider error" });
  }
});

// ─── Public: Omise webhook ──────────────────────────────────────────────────
// Omise doesn't sign webhook payloads the way Stripe does, so this checks a
// shared-secret query param configured on the webhook URL you register in
// the Omise dashboard, e.g.
//   https://your-api/api/billing/webhook?key=<OMISE_WEBHOOK_TOKEN>
// MVP: acknowledges known events and logs the rest. Extend as real recurring
// billing / chargeback handling is needed.
router.post("/billing/webhook", async (req, res): Promise<void> => {
  const expected = process.env["OMISE_WEBHOOK_TOKEN"];
  if (expected && req.query["key"] !== expected) {
    res.status(401).end();
    return;
  }

  logger.info({ event: req.body?.key }, "Omise webhook received");
  // TODO: on charge.complete with status=failed for a recurring charge,
  // mark guild_subscriptions.status = 'past_due'. Requires switching from
  // one-off manual charges to Omise Schedules for true auto-renewal.
  res.status(200).end();
});

export { isBillingDbConfigured };
export default router;
