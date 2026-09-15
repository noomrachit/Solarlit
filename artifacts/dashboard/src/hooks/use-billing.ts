import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";

// Raw-fetch hooks (same pattern as use-auth.ts) rather than the orval-generated
// client — billing isn't in lib/api-spec/openapi.yaml yet since it's a fast-
// moving scaffold; add it to the OpenAPI spec + regenerate once the shape settles.

export type PlanTier = "starter" | "standard" | "pro";

export interface PlanInfo {
  name: string;
  priceThb: number;
  relayBotLimit: number;
}

export interface BillingConfig {
  omiseConfigured: boolean;
  omisePublicKey: string | null;
  plans: Record<PlanTier, PlanInfo>;
}

export interface BillingStatus {
  tier: string;
  status: "trialing" | "active" | "past_due" | "canceled" | string;
  trialEndsAt: string | null;
  currentPeriodEnd: string | null;
  relayBotLimit: number;
}

async function fetchJson<T>(url: string, init?: RequestInit): Promise<T> {
  const res = await fetch(url, { credentials: "include", ...init });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.error || `Request failed (${res.status})`);
  }
  return res.json() as Promise<T>;
}

export function useBillingConfig() {
  return useQuery<BillingConfig>({
    queryKey: ["billing", "config"],
    queryFn: () => fetchJson("/api/billing/config"),
    staleTime: 5 * 60 * 1000,
  });
}

export function useBillingStatus(guildId: string) {
  return useQuery<BillingStatus>({
    queryKey: ["billing", "status", guildId],
    queryFn: () => fetchJson(`/api/billing/${guildId}/status`),
    enabled: Boolean(guildId),
  });
}

export function useSubscribe(guildId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (input: { tier: PlanTier; omiseToken: string }) =>
      fetchJson(`/api/billing/${guildId}/subscribe`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(input),
      }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["billing", "status", guildId] });
    },
  });
}

// Loads Omise.js once and opens its hosted card popup, resolving with a
// one-time card token. The raw card number never reaches our own code.
declare global {
  interface Window {
    OmiseCard?: {
      configure: (opts: { publicKey: string; currency?: string }) => void;
      open: (opts: {
        amount: number;
        defaultPaymentMethod?: string;
        onCreateTokenSuccess: (nonce: string) => void;
        onFormClosed?: () => void;
      }) => void;
    };
  }
}

let omiseScriptPromise: Promise<void> | null = null;

function loadOmiseScript(): Promise<void> {
  if (window.OmiseCard) return Promise.resolve();
  if (omiseScriptPromise) return omiseScriptPromise;
  omiseScriptPromise = new Promise((resolve, reject) => {
    const script = document.createElement("script");
    script.src = "https://cdn.omise.co/omise.js";
    script.onload = () => resolve();
    script.onerror = () => reject(new Error("โหลด Omise.js ไม่สำเร็จ"));
    document.head.appendChild(script);
  });
  return omiseScriptPromise;
}

export async function openOmiseCardForm(
  publicKey: string,
  amountSatang: number,
): Promise<string> {
  await loadOmiseScript();
  if (!window.OmiseCard) throw new Error("Omise.js โหลดไม่สำเร็จ");

  window.OmiseCard.configure({ publicKey, currency: "THB" });

  return new Promise((resolve, reject) => {
    window.OmiseCard!.open({
      amount: amountSatang,
      defaultPaymentMethod: "credit_card",
      onCreateTokenSuccess: (nonce) => resolve(nonce),
      onFormClosed: () => reject(new Error("ปิดฟอร์มบัตรก่อนกรอกเสร็จ")),
    });
  });
}
