/** Presentation shape only; schemas and receipt semantics belong to the owner. */
export interface ReceiptItem {
  recipient: string; kind: string; prepared: number; received: number;
  awaiting_receipt: number; unknown_items: number; replaying_received: number;
}
export interface DeliverySummary {
  owner: string; items: ReceiptItem[]; item_counts_pending: number; archive_import_pending?: number;
  incoming?: { sender: string; kind: string; received: number; awaiting_processing: number; unknown_items: number }[];
  incoming_counts_pending?: number;
  measured_at?: string; refreshing?: boolean; refresh_error?: string | null;
  [key: string]: unknown;
}

/** Return-path results are transport diagnostics, not another filtering stage. */
export function operatorReceiptItems<T extends { kind: string }>(items?: T[]): T[] {
  return (items ?? []).filter(item => ["delegation_changes", "snapshot_records", "page_jobs", "builder_candidates"].includes(item.kind));
}

export function parseDeliverySummary(value: unknown): DeliverySummary {
  if (!value || typeof value !== "object") throw new Error("Invalid receipt summary");
  const data = value as DeliverySummary;
  if (typeof data.owner !== "string" || !Array.isArray(data.items)
    || (data.measured_at !== undefined && (typeof data.measured_at !== "string" || !Number.isFinite(Date.parse(data.measured_at))))
    || (data.refreshing !== undefined && typeof data.refreshing !== "boolean")
    || !Number.isSafeInteger(data.item_counts_pending) || data.item_counts_pending < 0
    || (data.archive_import_pending !== undefined && (!Number.isSafeInteger(data.archive_import_pending) || data.archive_import_pending < 0))
    || (data.incoming_counts_pending !== undefined && (!Number.isSafeInteger(data.incoming_counts_pending) || data.incoming_counts_pending < 0))
    || (data.incoming !== undefined && (!Array.isArray(data.incoming) || data.incoming.some(item => typeof item.sender !== "string" || typeof item.kind !== "string"
      || [item.received, item.awaiting_processing, item.unknown_items].some(count => !Number.isSafeInteger(count) || count < 0))))
    || data.items.some(item => typeof item.recipient !== "string" || typeof item.kind !== "string"
      || [item.prepared, item.received, item.awaiting_receipt, item.unknown_items, item.replaying_received]
        .some(count => !Number.isSafeInteger(count) || count < 0)
      || item.prepared !== item.received + item.awaiting_receipt
      || item.replaying_received > item.received)) throw new Error("Invalid receipt counters");
  return data;
}

export function zoneReceiptTotals(data?: DeliverySummary) {
  if (!data || data.item_counts_pending > 0) return undefined;
  const items = data.items.filter(item => item.recipient === "base-screening"
    && ["delegation_changes", "snapshot_records"].includes(item.kind));
  if (items.some(item => item.unknown_items > 0)) return undefined;
  const totals = items.reduce((sum, item) => ({ received: sum.received + item.received,
    awaiting: sum.awaiting + item.awaiting_receipt }), { received: 0, awaiting: 0 });
  return { received: totals.received, awaiting: (data.archive_import_pending ?? 0) > 0 ? undefined : totals.awaiting };
}
