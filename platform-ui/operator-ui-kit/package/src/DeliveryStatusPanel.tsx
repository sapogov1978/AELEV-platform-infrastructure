import { useId, useState } from "react";
import type Keycloak from "keycloak-js";
import { EvidenceDialog } from "./EvidenceDialog";
import { useDeliverySnapshot } from "./useDeliverySnapshot";
import { operatorReceiptItems } from "./deliverySummary";

export function DeliveryStatusPanel({ keycloak, apiUrl, language, pollIntervalMs }: {
  keycloak: Keycloak; apiUrl: string; language: "ru" | "en"; pollIntervalMs?: number;
}) {
  const { data, error } = useDeliverySnapshot(keycloak, apiUrl, pollIntervalMs);
  const [open, setOpen] = useState(false);
  const id = useId();
  const ru = language === "ru";
  const incomplete = data && data.item_counts_pending > 0;
  const names: Record<string, string> = ru ? {
    "zone-ingestion": "Зоны",
    "base-screening": "Базовый отсев", "advance-screening": "Продвинутый отсев", "candidate-screening": "Продвинутый отсев",
    "deep-screening": "Экспертный отсев", "connector-builder": "Builder",
  } : { "zone-ingestion": "Zones", "base-screening": "Base screening", "advance-screening": "Advanced screening", "candidate-screening": "Advanced screening", "deep-screening": "Expert screening", "connector-builder": "Builder" };
  const kinds: Record<string, string> = ru ? {
    delegation_changes: "записей изменений", snapshot_records: "записей снимков",
    page_jobs: "заданий страниц", builder_candidates: "кандидатов", filter_results: "результатов",
  } : { delegation_changes: "change records", snapshot_records: "snapshot records", page_jobs: "page jobs", builder_candidates: "candidates", filter_results: "results" };
  return <section className="panel delivery-status">
    <h2>{ru ? "Передача между стадиями" : "Stage handoffs"}</h2>
    {!data && <p>{ru ? "Статистика загружается автоматически." : "Statistics are loading automatically."}</p>}
    {data?.measured_at && <small>{ru ? "Измерено" : "Measured"}: {new Date(data.measured_at).toLocaleTimeString()}{data.refreshing ? (ru ? " · обновляется" : " · refreshing") : ""}</small>}
    {error && <p role="status">{ru ? "Обновление задерживается; показаны последние полученные значения." : "Refresh delayed; showing the last received values."}</p>}
    {incomplete && <p>{ru ? "Идёт сверка сохранённой истории передач." : "Stored handoff history is being reconciled."}</p>}
    <div className="receipt-grid">{operatorReceiptItems(data?.incoming).map(item => <section key={`incoming:${item.sender}:${item.kind}`}>
      <h3>{ru ? "Вход от" : "Input from"} {names[item.sender] ?? item.sender} · {kinds[item.kind] ?? item.kind}</h3>
      <dl><div><dt>{ru ? "Принято, всего" : "Received, total"}</dt><dd><button className="metric-link" onClick={() => setOpen(true)}>{(data?.incoming_counts_pending ?? 0) > 0 || item.unknown_items > 0 ? "—" : item.received.toLocaleString()}</button></dd></div></dl>
    </section>)}</div>
    <div className="receipt-grid">{operatorReceiptItems(data?.items).map(item => <section key={`${item.recipient}:${item.kind}`}>
      <h3>{names[item.recipient] ?? item.recipient} · {kinds[item.kind] ?? item.kind}</h3>
      <dl>
        <div><dt>{ru ? "Передано — принято получателем, всего" : "Transferred — received, total"}</dt><dd><button className="metric-link" onClick={() => setOpen(true)}>{incomplete || item.unknown_items > 0 ? "—" : item.received.toLocaleString()}</button></dd></div>
        <div><dt>{ru ? "Ожидает передачи сейчас" : "Awaiting transfer now"}</dt><dd><button className="metric-link" onClick={() => setOpen(true)}>{incomplete || (data?.archive_import_pending ?? 0) > 0 || item.unknown_items > 0 ? "—" : item.awaiting_receipt.toLocaleString()}</button></dd></div>
      </dl>
    </section>)}</div>
    <button className="quiet compact" aria-haspopup="dialog" aria-controls={id} onClick={() => setOpen(true)}>{ru ? "Технические подробности" : "Technical details"}</button>
    {open && <EvidenceDialog id={id} title={ru ? "Подтверждения и восстановление передачи" : "Receipts and transfer recovery"} language={language} onClose={() => setOpen(false)}>
      <p>{ru ? "Передано означает устойчивый приём задания, не публикацию в Kafka и не завершение обработки. Неподтверждённые передачи повторяются автоматически с прежним идентификатором и байтами. Повтор и повторный запрос восстановления не увеличивают накопленный итог. Записи изменений, записи снимков, задания и кандидаты считаются отдельно; новые ревизии не являются новыми уникальными доменами." : "Transferred means durable receipt, not Kafka publication or completed processing. Unacknowledged deliveries replay automatically with identical IDs and bytes. Replays never increase the lifetime total. Changes, snapshots, jobs and candidates use separate units; new revisions are not new unique domains."}</p>
      <pre>{JSON.stringify({ ...data, refresh_error: error || undefined }, null, 2)}</pre>
    </EvidenceDialog>}
  </section>;
}
