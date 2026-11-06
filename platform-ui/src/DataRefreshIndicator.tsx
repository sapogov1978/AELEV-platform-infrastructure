import { useEffect, useState } from "react";
import type { Language } from "./types";
import { refreshExpired, type ReportSummary } from "./dataRefresh";

type Props = {
  language: Language;
  sourceLabel: string;
  pollSeconds: number | null;
  refreshing: boolean;
  failed: boolean;
  updatedAt: number | null;
  reports?: ReportSummary;
};

export function DataRefreshIndicator({ language, sourceLabel, pollSeconds, refreshing, failed, updatedAt, reports }: Props) {
  const [now, setNow] = useState(Date.now);
  useEffect(() => {
    const tick = () => setNow(Date.now());
    const timer = setInterval(tick, 1000);
    document.addEventListener("visibilitychange", tick);
    window.addEventListener("focus", tick);
    return () => { clearInterval(timer); document.removeEventListener("visibilitychange", tick); window.removeEventListener("focus", tick); };
  }, []);
  const ru = language === "ru";
  const state = refreshing ? "loading" : failed ? "error" : refreshExpired(updatedAt, pollSeconds, Math.max(now, Date.now())) ? "stale" : updatedAt === null ? "waiting" : reports?.total === 0 ? "empty" : reports && (reports.lost ?? 0) > 0 ? "disconnected" : reports && reports.unknown > 0 ? "incomplete" : "ready";
  const labels = ru
    ? { loading: "Получение данных…", error: "Ошибка обновления", ready: reports ? "Состояния получены" : "Обновлено", waiting: "Данные ещё не получены", stale: "Обновление задерживается", empty: "Нет активных публикаций", incomplete: "Есть неизвестные состояния", disconnected: "Потеряна связь с издателем" }
    : { loading: "Receiving data…", error: "Refresh failed", ready: reports ? "Status reports received" : "Updated", waiting: "No data received yet", stale: "Refresh overdue", empty: "No active publications", incomplete: "Some states are unknown", disconnected: "Publisher connection lost" };
  const interval = pollSeconds === null ? (ru ? "Режим обновления не сообщён" : "Refresh interval not reported") : pollSeconds === 60
    ? (ru ? "Данные обновляются раз в минуту" : "Data refreshes every minute")
    : (ru ? `Интервал обновления: ${pollSeconds} с` : `Refresh interval: ${pollSeconds} s`);
  const date = updatedAt !== null ? new Date(updatedAt) : null;
  const locale = ru ? "ru-RU" : "en-GB";
  const received = reports ? (ru ? "Последнее получение списка" : "Last list response") : (ru ? "Последнее успешное получение" : "Last successful refresh");
  const explanation = reports
    ? (ru ? `Источник: ${sourceLabel}. Время обозначает получение списка, не время отчёта сервиса. Неизвестных состояний: ${reports.unknown} из ${reports.total}.` : `Source: ${sourceLabel}. The timestamp is the list response time, not a service observation. Unknown states: ${reports.unknown} of ${reports.total}.`)
    : (ru ? `Источник: ${sourceLabel}. Состояние получения данных сообщает открытый раздел; свежесть сообщений каждого сервиса указана отдельно.` : `Source: ${sourceLabel}. The open section reports its data refresh state; each service report has its own freshness.`);

  return <div className={`data-refresh data-refresh--${state}`} role="status" aria-live="polite" aria-atomic="true" title={explanation}>
    <span className="data-refresh-dot" aria-hidden="true" />
    <span className="data-refresh-copy"><span className="data-refresh-label">{labels[state]}</span>
      {date && <span className="data-refresh-time">{!reports && state !== "ready" ? `${ru ? "Последнее:" : "Last:"} ` : ""}<time dateTime={date.toISOString()} title={`${received}: ${date.toLocaleString(locale)}`}>{date.toLocaleTimeString(locale)}</time></span>}
      <small>{interval}</small>
    </span>
  </div>;
}
