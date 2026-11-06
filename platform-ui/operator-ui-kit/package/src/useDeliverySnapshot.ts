import { useEffect, useState } from "react";
import type Keycloak from "keycloak-js";
import { createRefreshQueue, POLL_INTERVAL_MS } from "./polling";
import { parseDeliverySummary, type DeliverySummary } from "./deliverySummary";

export function receiptRetryDelay(header: string | null): number {
  const seconds = Number(header);
  return Number.isFinite(seconds) && seconds > 0 ? Math.min(60, Math.max(5, seconds)) * 1000 : 5000;
}

/** One serialized read per UI; short retries only while its snapshot warms. */
export function useDeliverySnapshot(
  keycloak: Keycloak,
  apiUrl: string,
  pollIntervalMs = POLL_INTERVAL_MS,
) {
  const [data, setData] = useState<DeliverySummary>();
  const [error, setError] = useState("");
  useEffect(() => {
    let active = true;
    let retry: number | undefined;
    let controller: AbortController | undefined;
    const enqueue = createRefreshQueue();
    const schedule = (delay: number) => {
      window.clearTimeout(retry);
      if (active) retry = window.setTimeout(() => void load(), delay);
    };
    const read = async () => {
      if (!active) return;
      window.clearTimeout(retry);
      controller = new AbortController();
      try {
        await keycloak.updateToken(30);
        if (!active) return;
        const response = await fetch(`${apiUrl}/delivery`, {
          headers: { Authorization: `Bearer ${keycloak.token}` },
          signal: AbortSignal.any([controller.signal, AbortSignal.timeout(10000)]),
        });
        if (!active) return;
        if (response.status === 503) {
          schedule(receiptRetryDelay(response.headers.get("Retry-After")));
          return;
        }
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        const snapshot = parseDeliverySummary(await response.json());
        if (!active) return;
        setData(snapshot); setError("");
        if (snapshot.refreshing) schedule(5000);
      } catch (failure) {
        if (active) { setError(String(failure)); schedule(60000); }
      }
    };
    const load = () => enqueue(read);
    void load();
    const interval = window.setInterval(() => void load(), pollIntervalMs);
    return () => { active = false; window.clearInterval(interval); window.clearTimeout(retry); controller?.abort(); };
  }, [keycloak, apiUrl, pollIntervalMs]);
  return { data, error };
}
