import type { Language } from "./modules";
import type { QueueInspection } from "./OwnedQueuePanel";

/** Presentation values supplied by the owner, not a domain or API contract. */
export type QueueProgress = {
  completedLastHour: number;
  completedLast24Hours: number;
  observedAt: string;
};

export function queueEtaHours(remaining: number, completedLast24Hours?: number): number | undefined {
  if (remaining === 0) return 0;
  return completedLast24Hours && completedLast24Hours > 0 ? remaining * 24 / completedLast24Hours : undefined;
}

function formatEta(hours: number | undefined, language: Language): string {
  const ru = language === "ru";
  if (hours === undefined) return ru ? "Нет данных для оценки" : "Not enough data to estimate";
  if (hours === 0) return ru ? "Очередь обработана" : "Queue processed";
  if (hours < 1 / 60) return ru ? "Менее минуты" : "Less than a minute";
  if (hours < 1) return `${Math.ceil(hours * 60)} ${ru ? "мин" : "min"}`;
  if (hours < 24) return `${Math.ceil(hours)} ${ru ? "ч" : "h"}`;
  return `${Math.ceil(hours / 24).toLocaleString(ru ? "ru-RU" : "en-US")} ${ru ? "дн" : "days"}`;
}

export function QueueResultHero({ language, name, total, completed, remaining, failed, progress, includesCancelledCompletions = false, onActivity }: {
  language: Language; name: string; total: number; completed: number; remaining: number; failed: number;
  progress?: QueueProgress; includesCancelledCompletions?: boolean; onActivity: (inspection: QueueInspection) => void;
}) {
  const ru = language === "ru";
  const count = (value: number) => value.toLocaleString(ru ? "ru-RU" : "en-US");
  const average = progress ? progress.completedLast24Hours / 24 : undefined;
  const eta = queueEtaHours(remaining, progress?.completedLast24Hours);
  const inspect = (selection: QueueInspection["selection"]) => () => onActivity({ selection, observedAt: progress?.observedAt });
  return <section className="result-hero stage-result">
    <span className="section-kicker">{ru ? `Главный результат ${name}` : `Primary ${name} result`}</span>
    <strong><button className="metric-link" data-queue-selection={includesCancelledCompletions ? "processed" : "completed"} onClick={inspect(includesCancelledCompletions ? "processed" : "completed")}>{count(completed)}</button></strong>
    <h2>{ru ? "заданий успешно обработано" : "jobs successfully processed"}</h2>
    <p>{ru ? `Из ${count(total)} сохранённых заданий этой стадии. Это обработки страниц, не уникальные домены и не передачи Builder.` : `Out of ${count(total)} stored jobs for this stage. These are page jobs, not unique domains or Builder handoffs.`}</p>
    <dl className="result-facts">
      <div><dt>{ru ? "Принято заданий, включая историю" : "Jobs received, including history"}</dt><dd><button className="metric-link" data-queue-selection="all" onClick={inspect("all")}>{count(total)}</button></dd></div>
      <div><dt>{ru ? "Обработано за последний час" : "Processed in the last hour"}</dt><dd><button className="metric-link" data-queue-selection="processed_hour" onClick={inspect("processed_hour")}>{progress ? `${count(progress.completedLastHour)} ${ru ? "заданий" : "jobs"}` : "—"}</button></dd></div>
      <div><dt>{ru ? "Средняя скорость за 24 часа" : "24-hour average throughput"}</dt><dd><button className="metric-link" data-queue-selection="processed_day" onClick={inspect("processed_day")}>{average === undefined ? "—" : `${average.toLocaleString(ru ? "ru-RU" : "en-US", { maximumFractionDigits: 2 })} ${ru ? "заданий/час" : "jobs/hour"}`}</button></dd></div>
      <div><dt>{ru ? "До обработки текущей очереди" : "Until the current queue is processed"}</dt><dd><button className="metric-link" data-queue-selection="active" onClick={inspect("active")}>{formatEta(eta, language)}</button></dd></div>
    </dl>
    <small>{ru ? `Осталось ${count(remaining)} ожидающих и выполняемых заданий. Прогноз — по среднему последних 24 часов, без новых поступлений; изменится со скоростью и числом воркеров. Повторы не увеличивают число завершений.` : `${count(remaining)} pending and running jobs remain. The estimate uses the last 24-hour average without new arrivals, and changes with throughput and worker count. Retries do not add completions.`}</small>
    <small>{includesCancelledCompletions ? (ru ? "Отзыв после завершения не стирает выполненную работу из скорости и итога; отмена до выполнения не считается обработкой. Ниже отдельно показаны действующие результаты." : "Withdrawal after completion does not erase performed work from throughput or the total; cancellation before execution is not processing. Current results are shown separately below.") : (ru ? "Старый API показывает только неотменённые завершения; это не полная история выполненной работы." : "The older API shows only non-cancelled completions, not the full history of performed work.")}</small>
    {failed > 0 && <p className="hint">{ru ? `${count(failed)} заданий с ошибкой не включены в прогноз и требуют восстановления.` : `${count(failed)} failed jobs are excluded from the estimate and require recovery.`}</p>}
    {progress && <small>{ru ? "Снимок счётчиков" : "Counters as of"}: {new Date(progress.observedAt).toLocaleString(ru ? "ru-RU" : "en-US")}</small>}
  </section>;
}
