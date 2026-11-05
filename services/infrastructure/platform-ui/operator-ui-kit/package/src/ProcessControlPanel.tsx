import { useCallback, useEffect, useState, type ReactNode } from "react";
import type Keycloak from "keycloak-js";
import { POLL_INTERVAL_MS, useSerialRefresh } from "./polling";
import { DeliveryStatusPanel } from "./DeliveryStatusPanel";

type Language = "ru" | "en";
export interface ProcessFact {
  role: string;
  desired_state: string;
  effective_state: string;
  revision: number;
  generation: number;
  completed_iterations: number;
  last_command_id?: string;
  updated_at: string;
}
interface Snapshot {
  processes: ProcessFact[];
  workers: Record<string, unknown>[];
  commands: Record<string, unknown>[];
  resource_scope: string;
  observed_at: string;
}
const states: Record<string, string> = {
  RUNNING: "Работает", PAUSED: "Приостановлен", DRAINING: "Завершает начатую работу",
  RESET_REQUESTED: "Ожидает перезапуска незавершённых заданий",
};
const deliveryRoles: Record<string, string> = {
  "durable-delivery": "Устойчивая доставка и воспроизведение",
  "snapshot-recovery": "Восстановление полного состава зоны",
};
export function processStateText(state: string, language: Language): string {
  return language === "ru" ? states[state] ?? state : state;
}

export function ProcessControlPanel({ keycloak, apiUrl, language, roleNames = {}, children, delivery = false, pollIntervalMs = POLL_INTERVAL_MS }: {
  keycloak: Keycloak; apiUrl: string; language: Language; roleNames?: Record<string, string>; children?: ReactNode; delivery?: boolean; pollIntervalMs?: number;
}) {
  const [snapshot, setSnapshot] = useState<Snapshot>();
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [rationale, setRationale] = useState("");
  const [offset, setOffset] = useState(0);
  const [history, setHistory] = useState<Record<string, unknown>[]>([]);
  const request = useCallback(async (path: string, init?: RequestInit) => {
    await keycloak.updateToken(30);
    const response = await fetch(`${apiUrl}${path}`, {
      ...init, headers: { Authorization: `Bearer ${keycloak.token}`, "Content-Type": "application/json", ...init?.headers },
    });
    if (!response.ok) throw new Error(`${response.status}: ${await response.text()}`);
    return response.json();
  }, [apiUrl, keycloak]);
  const load = useSerialRefresh(useCallback(async () => {
    try {
      const value: Snapshot = await request("/processes");
      setSnapshot(value);
      setError("");
    } catch (failure) { setError(String(failure)); }
  }, [request]));
  useEffect(() => {
    void load();
    const timer = window.setInterval(() => void load(), pollIntervalMs);
    return () => window.clearInterval(timer);
  }, [load, pollIntervalMs]);
  const command = async (item: ProcessFact, action: "PAUSE" | "RESUME" | "RESET") => {
    if (!rationale.trim() || busy) return;
    setBusy(true);
    try {
      await request(`/processes/${encodeURIComponent(item.role)}/commands`, {
        method: "POST", headers: { "Idempotency-Key": crypto.randomUUID() },
        body: JSON.stringify({ action, expected_revision: item.revision, rationale: rationale.trim() }),
      });
      await load();
      setOffset(0); setHistory([]);
    } catch (failure) { setError(String(failure)); }
    finally { setBusy(false); }
  };
  const historyPage = async (next: number) => {
    try {
      const page: { items: Record<string, unknown>[] } = await request(`/processes/commands?offset=${next}&limit=100`);
      setHistory(page.items); setOffset(next);
    } catch (failure) { setError(String(failure)); }
  };
  return <section className="panel process-controls">
    <h2>{language === "ru" ? "Управление процессами" : "Process controls"}</h2>
    <p>{language === "ru" ? "Пауза запрещает новые задания: уже начатая работа безопасно завершается. Перезапуск затрагивает только незавершённые и ошибочные задания. Готовые результаты и решения оператора сохраняются." : "Pause blocks new jobs; current work drains safely. Reset retries unfinished and failed jobs only. Completed results and operator decisions are preserved."}</p>
    <label>{language === "ru" ? "Причина команды" : "Command rationale"}<input value={rationale} maxLength={2000} onChange={(event) => setRationale(event.target.value)} /></label>
    <button className="quiet" disabled={busy} onClick={() => void load()}>{language === "ru" ? "Обновить состояние" : "Refresh state"}</button>
    {error && <p className="error" role="alert">{error}</p>}
    {!snapshot && !error && <p>{language === "ru" ? "Загрузка процессов…" : "Loading processes…"}</p>}
    {snapshot?.processes.map((item) => <article key={item.role}>
      <details><summary>{language === "ru" ? roleNames[item.role] ?? deliveryRoles[item.role] ?? item.role : item.role}: {processStateText(item.effective_state, language)} · {language === "ru" ? "поколение" : "generation"} {item.generation}</summary><pre>{JSON.stringify(item, null, 2)}</pre></details>
      <button disabled={busy || !rationale.trim() || item.desired_state === "PAUSED"} onClick={() => void command(item, "PAUSE")}>{language === "ru" ? "Пауза" : "Pause"}</button>
      <button disabled={busy || !rationale.trim() || item.desired_state === "RUNNING"} onClick={() => void command(item, "RESUME")}>{language === "ru" ? "Продолжить" : "Resume"}</button>
      <button disabled={busy || !rationale.trim() || item.desired_state === "RESET_REQUESTED"} onClick={() => void command(item, "RESET")}>{language === "ru" ? "Перезапустить незавершённые" : "Retry unfinished"}</button>
    </article>)}
    {snapshot && <>
      {children}
      {delivery && <DeliveryStatusPanel keycloak={keycloak} apiUrl={apiUrl} language={language} pollIntervalMs={pollIntervalMs} />}
      <details><summary>{language === "ru" ? "Ресурсы процессов и исходные данные" : "Process resources and raw data"} · {snapshot.observed_at}</summary><p>{language === "ru" ? "RSS, нити и дескрипторы измерены для ОС-процесса. Записи нескольких логических воркеров одного процесса нельзя суммировать. У каждой записи указано время наблюдения." : "RSS, threads and descriptors refer to the OS process. Do not sum logical workers sharing a process. Each record includes its observation time."}</p><pre>{JSON.stringify({ resource_scope: snapshot.resource_scope, workers: snapshot.workers, observed_at: snapshot.observed_at }, null, 2)}</pre></details>
      <details><summary>{language === "ru" ? "История команд" : "Command history"}</summary><pre>{JSON.stringify(offset === 0 ? snapshot.commands : history, null, 2)}</pre><button disabled={offset === 0} onClick={() => void historyPage(Math.max(0, offset - 100))}>{language === "ru" ? "Новее" : "Newer"}</button><button disabled={(offset === 0 ? snapshot.commands : history).length < 100} onClick={() => void historyPage(offset + 100)}>{language === "ru" ? "Старше" : "Older"}</button></details>
    </>}
  </section>;
}
