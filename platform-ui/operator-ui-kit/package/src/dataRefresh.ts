export type DataRefreshPhase = "LOADING" | "READY" | "ERROR";

export type DataRefreshEvent =
  | { phase: "LOADING" }
  | { phase: "READY"; at: string }
  | { phase: "ERROR"; error: string };

export interface DataRefreshStatus {
  phase: DataRefreshPhase;
  updatedAt?: string;
  error?: string;
}

export type DataRefreshReporter = (event: DataRefreshEvent) => void;

export function mergeDataRefreshEvent(previous: DataRefreshStatus | undefined, event: DataRefreshEvent): DataRefreshStatus {
  if (event.phase === "READY") return { phase: "READY", updatedAt: event.at };
  if (event.phase === "ERROR") return { phase: "ERROR", updatedAt: previous?.updatedAt, error: event.error };
  return { phase: "LOADING", updatedAt: previous?.updatedAt };
}
