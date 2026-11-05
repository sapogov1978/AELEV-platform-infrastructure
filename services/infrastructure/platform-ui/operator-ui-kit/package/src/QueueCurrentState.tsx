import { useId, useState } from "react";
import { EvidenceDialog } from "./EvidenceDialog";
import type { Language } from "./modules";
import type { QueueInspection, QueueSelection } from "./OwnedQueuePanel";
import type { QueueSummary } from "./QueueServiceApp";

/** Presentation values validated and supplied by the owning application. */
export type QueueOutcomes = { accepted: number; rejected: number; uncertain: number; other: number };
export type QueueRoutes = { accepted: Record<Language, string>; uncertain: Record<Language, string> };

export function QueueCurrentState({ language, title, data, routes, onActivity }: {
  language: Language; title: string; data: QueueSummary; routes: QueueRoutes; onActivity?: (inspection: QueueInspection) => void;
}) {
  const ru = language === "ru";
  const [evidenceOpen, setEvidenceOpen] = useState(false);
  const evidenceId = useId();
  const count = (value?: number) => value === undefined ? "—" : value.toLocaleString(ru ? "ru-RU" : "en-US");
  const cancelled = data.cancelled ?? 0;
  const activeTotal = data.total - cancelled;
  const percent = activeTotal > 0 ? data.completed / activeTotal * 100 : 0;
  const inspect = (selection: QueueSelection) => { if (onActivity) onActivity({ selection }); else setEvidenceOpen(true); };
  const metric = (value: number | undefined, selection: QueueSelection) => <button className="metric-link" data-queue-selection={selection} onClick={() => inspect(selection)}>{count(value)}</button>;
  return <section className="panel queue-current-state">
    <div className="overview-heading"><div>
      <span className="section-kicker">{ru ? "Текущее состояние" : "Current state"}</span>
      <h2>{ru ? `Как идёт ${title.toLocaleLowerCase("ru-RU")}` : `${title} progress`}</h2>
      <p>{ru ? "Вход, результаты проверки и техническое состояние собственной очереди." : "Input, screening outcomes and the technical state of this service's queue."}</p>
    </div></div>
    <div className="process-flow" aria-label={ru ? "Состояние стадии отсева" : "Screening stage state"}>
      <section className="flow-card">
        <span>01 · {ru ? "Вход" : "Input"}</span>
        <strong>{metric(data.total, "all")}</strong>
        <p>{ru ? "заданий сохранено в этой стадии за всю историю" : "jobs stored by this stage across its history"}</p>
        <dl className="queue-state-breakdown">
          <div><dt>{ru ? "Ожидают проверки" : "Waiting for screening"}</dt><dd>{metric(data.pending, "pending")}</dd></div>
          <div><dt>{ru ? "В работе" : "Running"}</dt><dd>{metric(data.running, "running")}</dd></div>
        </dl>
        <small>{ru ? "Это задания страниц, не уникальные домены. Отменённые записи входят во входящий итог, но не в остаток очереди." : "These are page jobs, not unique domains. Cancelled records count toward input, but not remaining work."}</small>
      </section>
      <section className="flow-card primary">
        <span>02 · {ru ? "Отсев" : "Screening"}</span>
        <strong><button className="metric-link" data-queue-selection="completed" onClick={() => inspect("completed")}>{count(data.completed)} <small>{percent.toLocaleString(ru ? "ru-RU" : "en-US", { maximumFractionDigits: 3 })}%</small></button></strong>
        <p>{ru ? `заданий завершено из ${count(activeTotal)} неотменённых` : `jobs completed out of ${count(activeTotal)} non-cancelled jobs`}</p>
        <dl className="queue-state-breakdown">
          <div><dt>{ru ? "Отсеяно моделью" : "Rejected by the model"}</dt><dd>{metric(data.outcomes?.rejected, "rejected")}</dd></div>
          <div><dt>{routes.accepted[language]}</dt><dd>{metric(data.outcomes?.accepted, "accepted")}</dd></div>
          <div><dt>{routes.uncertain[language]}</dt><dd>{metric(data.outcomes?.uncertain, "uncertain")}</dd></div>
          {data.outcomeDetails ? <>
            {data.outcomeDetails.historicalResearch > 0 && <div><dt>{ru ? "Историческое техническое исследование завершено" : "Historical technical research completed"}</dt><dd>{metric(data.outcomeDetails.historicalResearch, "historical_research")}</dd></div>}
            {data.outcomeDetails.missing > 0 && <div><dt>{ru ? "Завершено без записанного исхода" : "Completed without a recorded outcome"}</dt><dd>{metric(data.outcomeDetails.missing, "missing_outcome")}</dd></div>}
            {data.outcomeDetails.unsupported > 0 && <div><dt>{ru ? "Недопустимый исход — требуется проверка" : "Unsupported outcome — inspection required"}</dt><dd>{metric(data.outcomeDetails.unsupported, "unsupported_outcome")}</dd></div>}
          </> : <div><dt>{ru ? "Ответы без расшифровки старого API" : "Unexplained outcomes from the older API"}</dt><dd>{metric(data.outcomes?.other, "other")}</dd></div>}
        </dl>
        <small>{ru ? "Действующие результаты проверки. Подтверждённые передачи показаны отдельно." : "Current screening outcomes. Confirmed handoffs are shown separately."}</small>
        {!data.outcomes && <small>{ru ? "Разбивка результатов ещё не получена от API." : "The API has not supplied an outcome breakdown yet."}</small>}
        <div className="progress-track"><i style={{ width: `${Math.max(0, Math.min(100, percent))}%` }} /></div>
      </section>
      <section className={data.failed > 0 ? "flow-card attention" : "flow-card"}>
        <span>03 · {ru ? "Контроль" : "Control"}</span>
        <strong>{metric(data.failed, "failed")}</strong>
        <p>{ru ? "заданий с технической ошибкой, не тематическим отказом" : "jobs with technical failures, not thematic rejections"}</p>
        <dl className="queue-state-breakdown">
          <div><dt>{ru ? "Отменённые задания" : "Cancelled jobs"}</dt><dd>{metric(cancelled, "cancelled")}</dd></div>
          {data.cancellationDetails && <>
            <div><dt>{ru ? "Отменены после обработки — результат сохранён" : "Cancelled after processing — result preserved"}</dt><dd>{metric(data.cancellationDetails.after, "cancelled_processed")}</dd></div>
            <div><dt>{ru ? "Отменены до обработки — проверить замену" : "Cancelled before processing — check replacement"}</dt><dd>{metric(data.cancellationDetails.before, "cancelled_unprocessed")}</dd></div>
          </>}
        </dl>
        <small>{ru ? "Самое старое незавершённое задание" : "Oldest unfinished job"}: <button className="metric-link" data-queue-selection="active" onClick={() => inspect("active")}>{data.oldest ? new Date(data.oldest).toLocaleString(ru ? "ru-RU" : "en-US") : "—"}</button></small>
        <small>{ru ? "Ошибки требуют восстановления; отмены сохраняются как история и не означают отрицательное решение." : "Failures require recovery; cancellations remain in history and do not mean a negative decision."}</small>
      </section>
    </div>
    <button className="quiet compact" aria-haspopup="dialog" aria-controls={evidenceId} onClick={() => setEvidenceOpen(true)}>{ru ? "Подробности показателей" : "Counter details"}</button>
    {evidenceOpen && <EvidenceDialog id={evidenceId} title={ru ? "Исходные счётчики владельца и пояснение" : "Owner's original counters and explanation"} language={language} onClose={() => setEvidenceOpen(false)}>
      <p>{ru ? "Здесь показаны действующие задания COMPLETED. ACCEPT, REJECT и REVIEW — ответы фильтра; историческое техническое исследование с исходом COMPLETED не является положительным тематическим решением. Отсутствующий или недопустимый исход указан отдельно и не считается отказом или принятием. Все счётчики получены из одного снимка собственной очереди, независимо от видимой страницы. Человеческое решение и фактическая доставка проверяются в соответствующих журналах." : "This panel shows current COMPLETED jobs. ACCEPT, REJECT and REVIEW are filter answers; historical technical research with outcome COMPLETED is not a positive thematic decision. Missing or unsupported outcomes are separate, never acceptance or rejection. All counters come from one snapshot of the owner's queue, independent of the visible page. Human decisions and actual delivery are verified in their respective journals."}</p>
      <pre>{JSON.stringify(data.raw, null, 2)}</pre>
    </EvidenceDialog>}
  </section>;
}
