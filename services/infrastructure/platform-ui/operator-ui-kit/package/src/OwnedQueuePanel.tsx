import { useCallback, useEffect, useRef, useState } from "react";
import { POLL_INTERVAL_MS, useSerialRefresh } from "./polling";

export interface QueuePresentation {
  rows: { id: string; label: string; state: string; reviewUrl?: string; handoffUrl?: string; recheckUrl?: string }[];
  facts: unknown;
  total?: number;
}

export type QueueSelection = "all" | "active" | "pending" | "running" | "failed" | "cancelled" | "cancelled_processed" | "cancelled_unprocessed" | "completed" | "accepted" | "rejected" | "uncertain" | "historical_research" | "missing_outcome" | "unsupported_outcome" | "other" | "processed" | "processed_hour" | "processed_day";
export type QueueInspection = { selection: QueueSelection; observedAt?: string };
export const queueSelectionLabel = (selection: QueueSelection, ru: boolean): string => ({
  all: ru ? "Все задания, включая историю" : "All jobs, including history",
  active: ru ? "Незавершённая очередь" : "Unfinished queue", pending: ru ? "Ожидают проверки" : "Waiting",
  running: ru ? "В работе" : "Running", failed: ru ? "Технические ошибки" : "Technical failures",
  cancelled: ru ? "Отменённые задания" : "Cancelled jobs", cancelled_processed: ru ? "Отменены после обработки" : "Cancelled after processing",
  cancelled_unprocessed: ru ? "Отменены до обработки" : "Cancelled before processing",
  completed: ru ? "Действующие завершённые результаты" : "Current completed results",
  accepted: ru ? "Приняты моделью" : "Accepted by the model", rejected: ru ? "Отсеяны моделью" : "Rejected by the model",
  uncertain: ru ? "Неопределённые результаты" : "Uncertain results", historical_research: ru ? "Историческое техническое исследование" : "Historical research",
  missing_outcome: ru ? "Завершения без исхода" : "Missing outcomes", unsupported_outcome: ru ? "Недопустимые исходы" : "Unsupported outcomes",
  other: ru ? "Завершения без бинарного решения" : "Non-binary completions",
  processed: ru ? "Вся выполненная работа" : "All processed work", processed_hour: ru ? "Обработано за час снимка" : "Processed in the snapshot hour",
  processed_day: ru ? "Обработано за сутки снимка" : "Processed in the snapshot day",
})[selection];

/** Presentation only; endpoints and domain facts belong to the caller. */
export function OwnedQueuePanel({ language, load, details, evidence, evidenceText, inspection, detailFacts }: {
  language: "ru" | "en"; load: (offset: number, activeOnly: boolean, inspection?: QueueInspection) => Promise<QueuePresentation>;
  details: (id: string) => Promise<unknown>; evidence: (id: string) => Promise<unknown>;
  evidenceText?: (value: unknown) => string | undefined;
  inspection?: QueueInspection;
  detailFacts?: (value: unknown, language: "ru" | "en") => { label: string; value: string }[];
}) {
  const [snapshot, setSnapshot] = useState<QueuePresentation>();
  const [offset, setOffset] = useState(0);
  const [activeOnly, setActiveOnly] = useState(true);
  const [selected, setSelected] = useState<string>();
  const [raw, setRaw] = useState<unknown>();
  const [sourceView, setSourceView] = useState(false);
  const [error, setError] = useState("");
  const ru = language === "ru";
  const readSequence = useRef(0);
  const refreshSequence = useRef(0);
  useEffect(() => () => { ++readSequence.current; ++refreshSequence.current; }, []);
  const refresh = useSerialRefresh(useCallback(async () => {
    const sequence = refreshSequence.current;
    try { const data = await load(offset, activeOnly, inspection); if (sequence === refreshSequence.current) { setSnapshot(data); setSelected(current => data.rows.some(row => row.id === current) ? current : undefined); setError(""); } }
    catch (failure) { if (sequence === refreshSequence.current) setError(String(failure)); }
  }, [load, offset, activeOnly, inspection]));
  useEffect(() => { void refresh(); const timer = window.setInterval(() => void refresh(), POLL_INTERVAL_MS); return () => clearInterval(timer); }, [refresh]);
  const open = async (id: string, source = false) => {
    const sequence = ++readSequence.current;
    try { setSelected(id); setRaw(undefined); setSourceView(source); setError(""); const result = await (source ? evidence(id) : details(id)); if (sequence === readSequence.current) { setRaw(result); setError(""); } }
    catch (failure) { if (sequence === readSequence.current) setError(String(failure)); }
  };
  const changePage = (nextOffset: number, nextActiveOnly: boolean) => {
    ++readSequence.current; ++refreshSequence.current;
    setSelected(undefined); setRaw(undefined); setSnapshot(undefined);
    setOffset(nextOffset); setActiveOnly(nextActiveOnly);
  };
  return <div className="filter-panel-stack">
    {error && <p role="alert" className="error">{error}</p>}
    <section className="panel"><h2>{inspection ? queueSelectionLabel(inspection.selection, ru) : (ru ? "Работа сервиса" : "Service activity")}</h2><p>{["active", "pending", "running"].includes(inspection?.selection ?? "active") ? (ru ? "Сначала наиболее старые задания; новые добавляются в конец." : "Oldest jobs first; new work joins the end.") : (ru ? "Последние результаты сверху. Входная очередь находится в отдельной вкладке." : "Recent results first. Incoming work has a separate tab.")}</p>{inspection?.selection.startsWith("cancelled") && <p>{ru ? "Отмена сохраняет историю. Здесь есть прежние ошибочные самоотмены при переходе между фильтрами и заменённые попытки. Сохранённый результат означает, что работа выполнена; отсутствие результата само по себе не доказывает потерю — могла быть создана новая попытка. Старый CANCEL не воскрешается: проверяется текущий путь и назначается новая проверка только при отсутствии замены и человеческого запрета." : "Cancellation preserves history, including earlier erroneous self-cancellations on stage transitions and superseded attempts. A stored result proves processing; an absent result alone does not prove loss, since a replacement may exist. Old CANCEL tombstones are not revived; only an unfinished current path without a replacement or human stop can be rechecked."}</p>}<button className="quiet compact" onClick={() => void refresh()}>{ru ? "Обновить" : "Refresh"}</button><details><summary>{ru ? "Счётчики и исходные данные очереди" : "Queue counters and raw data"}</summary><pre>{JSON.stringify(snapshot?.facts, null, 2)}</pre></details></section>
    <section className="workspace review-workspace">
      <aside><h3>{ru ? "Задания" : "Jobs"} · {snapshot?.total ?? snapshot?.rows.length ?? "…"}</h3>{!inspection && <label className="checkbox-field"><input type="checkbox" checked={activeOnly} onChange={event => changePage(0, event.target.checked)} />{ru ? "Только незавершённые и ошибочные" : "Unfinished and failed only"}</label>}
        <div className="queue">{snapshot?.rows.map(row => <button className={`card ${selected === row.id ? "selected" : ""}`} key={row.id} onClick={() => void open(row.id)}><strong>{row.label}</strong><span>{row.state}</span><i>{row.id}</i></button>)}</div>
        {snapshot?.rows.length === 0 && <p className="empty">{ru ? "Заданий по этому фильтру нет." : "No jobs match this filter."}</p>}
        <div className="pagination"><button className="quiet compact" disabled={offset === 0} onClick={() => changePage(Math.max(0, offset - 100), activeOnly)}>{ru ? "Предыдущие" : "Previous"}</button><button className="quiet compact" disabled={snapshot?.rows.length !== 100} onClick={() => changePage(offset + 100, activeOnly)}>{ru ? "Следующие" : "Next"}</button></div>
      </aside>
      <article>{selected ? <>
        <section className="panel"><h2>{snapshot?.rows.find(row => row.id === selected)?.label ?? selected}</h2>{snapshot?.rows.filter(row => row.id === selected).map(row => <div className="actions" key={row.id}>
          {/^https?:\/\//.test(row.label) && <a href={row.label} target="_blank" rel="noopener noreferrer">{ru ? "Открыть страницу" : "Open page"}</a>}
          <button className="quiet" onClick={() => void open(row.id, true)}>{ru ? "Открыть сохранённый документ" : "Open saved document"}</button>
          {row.reviewUrl && <a href={row.reviewUrl} target="_blank" rel="noopener noreferrer">{ru ? "Пересмотреть решение этой стадии" : "Review this stage's decision"}</a>}
          {row.recheckUrl && <a href={row.recheckUrl} target="_blank" rel="noopener noreferrer">{ru ? "Текущий путь и повторная проверка" : "Current path and reassessment"}</a>}
          {row.handoffUrl && <a href={row.handoffUrl} target="_blank" rel="noopener noreferrer">{ru ? "Просмотреть или отозвать передачу" : "Review or revoke handoff"}</a>}
        </div>)}</section>
        {detailFacts && !sourceView && <section className="panel"><h3>{ru ? "Результат и причина" : "Result and reason"}</h3><dl>{detailFacts(raw, language).map(fact => <div key={fact.label}><dt>{fact.label}</dt><dd>{fact.value}</dd></div>)}</dl></section>}
        {evidenceText?.(raw) && <section className="panel"><h3>{ru ? "Сохранённый оригинал" : "Saved original"}</h3><pre>{evidenceText(raw)}</pre></section>}
        <details className="panel technical"><summary>{ru ? "Исходные данные задания или документа" : "Raw job or document evidence"}</summary><pre>{typeof raw === "string" ? raw : JSON.stringify(raw, null, 2)}</pre></details>
      </> : <p className="empty">{ru ? "Выберите задание слева." : "Select a job on the left."}</p>}</article>
    </section>
  </div>;
}
