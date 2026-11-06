import { useCallback, useMemo } from "react";

export const POLL_INTERVAL_MS = 5 * 60 * 1000;

// Promise.all rejects early, leaving sibling reads in flight. Wait for all
// reads before releasing the page's refresh slot, including on partial failure.
export async function settleReads<T extends readonly unknown[]>(reads: { [K in keyof T]: Promise<T[K]> }): Promise<T> {
  const results = await Promise.allSettled(reads);
  const failed = results.find((result) => result.status === "rejected");
  if (failed?.status === "rejected") throw failed.reason;
  return results.map((result) => result.status === "fulfilled" ? result.value : undefined) as unknown as T;
}

// Keep at most one refresh in flight and one (latest) trailing refresh.
// A refresh requested after a mutation must not be silently discarded.
export function createRefreshQueue() {
  let pending: (() => Promise<void>) | undefined;
  let running: Promise<void> | undefined;
  return (refresh: () => Promise<void>): Promise<void> => {
    pending = refresh;
    if (!running) {
      running = Promise.resolve().then(async () => {
        try {
          while (pending) {
            const next = pending;
            pending = undefined;
            await next();
          }
        } finally {
          pending = undefined;
          running = undefined;
        }
      });
    }
    return running;
  };
}

export function useSerialRefresh<Args extends unknown[]>(refresh: (...args: Args) => Promise<void>) {
  const enqueue = useMemo(createRefreshQueue, []);
  return useCallback((...args: Args) => enqueue(() => refresh(...args)), [enqueue, refresh]);
}
