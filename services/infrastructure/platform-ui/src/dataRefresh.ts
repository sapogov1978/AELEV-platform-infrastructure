import type { Module } from "./types";

export type ReportSummary = { total: number; unknown: number; lost?: number };

export function reportSummary(modules: Module[]): ReportSummary {
  const states = modules.flatMap(module => [module.status.state, ...(module.components ?? []).map(component => module.status.components?.find(report => report.id === component.id)?.state ?? "UNKNOWN")]);
  return { total: states.length, unknown: states.filter(state => state === "UNKNOWN").length, lost: modules.filter(module => module.status.connection === "LOST").length };
}

export function refreshExpired(updatedAt: number | null, pollSeconds: number | null, now: number): boolean {
  return updatedAt !== null && pollSeconds !== null && now - updatedAt >= 2 * pollSeconds * 1000;
}

export type RefreshState = {
  sequence: number;
  refreshing: boolean;
  failed: boolean;
  updatedAt: number | null;
  pollSeconds: number | null;
};

export const UNKNOWN_REFRESH: RefreshState = { sequence: 0, refreshing: false, failed: false, updatedAt: null, pollSeconds: null };

export function ownerRefreshMessage(event: MessageEvent, frame: Window | null, url: string, previous: RefreshState, now = Date.now()): RefreshState | null {
  if (!frame || event.source !== frame || event.origin !== new URL(url).origin) return null;
  const data = event.data as Record<string, unknown> | null;
  if (!data || typeof data !== "object" || Object.keys(data).sort().join() !== "phase,poll_seconds,sequence,type,updated_at,version" || data.type !== "AELEV_UI_REFRESH" || data.version !== 1 || typeof data.phase !== "string" || !["LOADING", "READY", "ERROR"].includes(data.phase) || !Number.isSafeInteger(data.sequence) || (data.sequence as number) <= previous.sequence || !Number.isSafeInteger(data.poll_seconds) || (data.poll_seconds as number) <= 0) return null;
  if (data.updated_at !== null && (typeof data.updated_at !== "string" || !/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,3})?(?:Z|[+-]\d{2}:\d{2})$/.test(data.updated_at))) return null;
  const timestamp = data.updated_at === null ? null : Date.parse(data.updated_at as string);
  if (timestamp !== null && (!Number.isFinite(timestamp) || timestamp > now + 60_000)) return null;
  if (timestamp !== null) {
    const day = (data.updated_at as string).slice(0, 10);
    const midnight = Date.parse(day + "T00:00:00Z");
    if (!Number.isFinite(midnight) || new Date(midnight).toISOString().slice(0, 10) !== day) return null;
  }
  if (data.phase === "READY" && timestamp === null) return null;
  if (data.phase !== "READY" && timestamp !== null) return null;
  return { sequence: data.sequence as number, refreshing: data.phase === "LOADING", failed: data.phase === "ERROR", updatedAt: timestamp ?? previous.updatedAt, pollSeconds: data.poll_seconds as number };
}
