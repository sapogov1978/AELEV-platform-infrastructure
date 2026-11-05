import type { DataRefreshStatus } from "./dataRefresh";
import type { Language } from "./modules";

export function DataRefreshIndicator({ status, language, label }: { status: DataRefreshStatus; language: Language; label: string }) {
  const ru = language === "ru";
  const title = status.phase === "ERROR" ? (ru ? "Данные не обновлены" : "Data not refreshed")
    : status.phase === "READY" ? (ru ? "Данные актуальны" : "Data is current")
    : (ru ? "Получаем данные…" : "Fetching data…");
  const time = status.updatedAt ? new Date(status.updatedAt).toLocaleString(ru ? "ru-RU" : "en-GB") : undefined;
  const detail = time ? `${status.phase === "LOADING" ? (ru ? "последние данные" : "last data") : (ru ? "обновлено" : "updated")} ${time}`
    : status.phase === "ERROR" ? (status.error ?? (ru ? "ошибка первого получения" : "initial fetch failed"))
    : (ru ? "первое получение ещё не завершено" : "initial fetch has not completed");
  return <div className="header-data-status" role="status" aria-live="polite" title={status.error}>
    <span className={`status-dot ${status.phase === "ERROR" ? "failed" : status.phase === "READY" ? "online" : "waiting"}`} />
    <div><span className="data-status-scope">{label}</span><strong>{title}</strong><span>{detail}</span></div>
  </div>;
}
