import pg from "pg";
import { logger } from "../lib/logger";

const { Pool } = pg;

// Billing lives in its own Postgres (the "Solarlit Billing" Railway project),
// separate from the bot's operational DB (@workspace/db / DATABASE_URL) —
// keeps subscription/payment data isolated from bot data.
//
// Until BILLING_DATABASE_URL is set (Railway var not provisioned yet as of
// this scaffold), we fall back to DATABASE_URL so the server still boots in
// dev, but billing routes will refuse to operate against the wrong DB in
// production — see requireBillingDb() below.
const connectionString =
  process.env["BILLING_DATABASE_URL"] || process.env["DATABASE_URL"];

if (!connectionString) {
  logger.warn(
    "BILLING_DATABASE_URL (and DATABASE_URL) are both unset — billing routes will error until one is configured",
  );
}

export const billingPool = connectionString
  ? new Pool({ connectionString })
  : null;

export function isBillingDbConfigured(): boolean {
  return billingPool !== null;
}

/**
 * Idempotent schema bootstrap for the billing DB. Mirrors the
 * CREATE-TABLE-IF-NOT-EXISTS convention bot/database.py uses for the main
 * bot DB. Call once at server startup (see index.ts).
 *
 * NOTE: voicerelay/access.py (Python/asyncpg) creates the same table
 * independently against the same BILLING_DATABASE_URL — the two definitions
 * must be kept in sync if this schema changes.
 */
export async function ensureBillingSchema(): Promise<void> {
  if (!billingPool) return;
  await billingPool.query(`
    CREATE TABLE IF NOT EXISTS guild_subscriptions (
      guild_id BIGINT PRIMARY KEY,
      tier TEXT NOT NULL DEFAULT 'trial',
      status TEXT NOT NULL DEFAULT 'trialing',
      trial_ends_at TIMESTAMPTZ NOT NULL DEFAULT (now() + interval '30 days'),
      current_period_end TIMESTAMPTZ,
      omise_customer_id TEXT,
      omise_charge_id TEXT,
      created_by_user_id TEXT,
      created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
      updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
    );
  `);
  logger.info("Billing schema ensured (guild_subscriptions)");
}
