import { logger } from "./logger";

// Thin wrapper over Omise's REST API (https://www.omise.co/api). No SDK
// dependency — Omise's API is plain HTTPS + HTTP Basic Auth (secret key as
// the username, empty password), so `fetch` is enough and keeps this
// scaffold dependency-free until real keys exist.
//
// OMISE_PUBLIC_KEY / OMISE_SECRET_KEY are NOT set yet (scaffold — see
// billing.ts route comments). Every function here throws a clear error if
// called before they're configured, instead of silently doing nothing.

const OMISE_API = "https://api.omise.co";

export function getOmisePublicKey(): string | null {
  return process.env["OMISE_PUBLIC_KEY"] || null;
}

function getOmiseSecretKey(): string {
  const key = process.env["OMISE_SECRET_KEY"];
  if (!key) {
    throw new Error(
      "OMISE_SECRET_KEY is not set — billing is not configured yet",
    );
  }
  return key;
}

export function isOmiseConfigured(): boolean {
  return Boolean(process.env["OMISE_SECRET_KEY"]) && Boolean(getOmisePublicKey());
}

function authHeader(secretKey: string): string {
  return "Basic " + Buffer.from(`${secretKey}:`).toString("base64");
}

async function omiseRequest<T>(
  path: string,
  init: RequestInit,
): Promise<T> {
  const secretKey = getOmiseSecretKey();
  const res = await fetch(`${OMISE_API}${path}`, {
    ...init,
    headers: {
      ...init.headers,
      Authorization: authHeader(secretKey),
      "Content-Type": "application/x-www-form-urlencoded",
    },
  });
  const body = (await res.json()) as T & { object?: string; message?: string };
  if (!res.ok || body.object === "error") {
    logger.error({ omiseError: body }, "Omise API error");
    throw new Error(body.message || "Omise API request failed");
  }
  return body;
}

interface OmiseCustomer {
  id: string;
  email: string | null;
}

interface OmiseCharge {
  id: string;
  status: string; // "successful" | "failed" | "pending" | ...
  amount: number;
  currency: string;
}

/** Create (or you could later look up + reuse) an Omise customer for a card token. */
export async function createOmiseCustomer(
  cardToken: string,
  email?: string,
): Promise<OmiseCustomer> {
  return omiseRequest<OmiseCustomer>("/customers", {
    method: "POST",
    body: new URLSearchParams({
      card: cardToken,
      ...(email ? { email } : {}),
    }),
  });
}

/** Charge a previously-created customer's default card. Amount is in satang (THB * 100). */
export async function chargeCustomer(
  customerId: string,
  amountSatang: number,
  description: string,
): Promise<OmiseCharge> {
  return omiseRequest<OmiseCharge>("/charges", {
    method: "POST",
    body: new URLSearchParams({
      amount: String(amountSatang),
      currency: "thb",
      customer: customerId,
      description,
    }),
  });
}
