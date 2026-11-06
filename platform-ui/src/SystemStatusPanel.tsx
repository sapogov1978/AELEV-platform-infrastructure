import { useState } from "react";
import type { ExternalLinks, Health, Language, Module, Title } from "./types";

function OpenLink({ language, module, links, title }: { language: Language; module: Module; links: ExternalLinks; title: Title }) {
  const swaggerOnly = module.service_kind === "FUNCTIONAL" && module.sections.length > 0;
  const url = swaggerOnly ? links.swagger_url : links.ui_url ?? links.swagger_url;
  if (!url || url.length > 2048 || /[\s\x00-\x1f\x7f\\]/.test(url)) return <td />;
  try {
    const parsed = new URL(url);
    const localHttp = parsed.protocol === "http:" && parsed.port !== "0" && /^http:\/\/(localhost|127\.0\.0\.1|\[::1\])(?::[0-9]+)?(?:[/?]|$)/i.test(url);
    if ((parsed.protocol !== "https:" && !localHttp) || !parsed.hostname || parsed.username || parsed.password || url.includes("#") || url.split("/")[2]?.includes("@")) return <td />;
  } catch { return <td />; }
  const swagger = swaggerOnly || !links.ui_url;
  const label = language === "ru" ? "Открыть" : "Open";
  const hint = `${label}: ${title[language]} · ${swagger ? "Swagger" : "UI"}`;
  return <td className="status-actions"><a className="status-open" href={url} target="_blank" rel="noopener noreferrer" referrerPolicy="no-referrer" aria-label={hint} title={hint}>{label}<span aria-hidden="true"> ↗</span></a></td>;
}

function StatusCells({ language, health, unavailable }: { language: Language; health: Health; unavailable: boolean }) {
  const ru = language === "ru";
  const labels = { ONLINE: ru ? "Доступен" : "Online", OFFLINE: ru ? "Недоступен" : "Offline", UNKNOWN: ru ? "Нет данных" : "Unknown" };
  const lost = !unavailable && health.connection === "LOST";
  const state = unavailable ? "UNKNOWN" : lost ? health.reported_state ?? health.state : health.state;
  return <><td><span className={"status-pill " + state.toLowerCase()}>{labels[state]}</span>{lost && <span className="publisher-connection lost">{ru ? "Нет связи с издателем" : "Publisher connection lost"}</span>}</td><td className="aelev-muted">{health.observed_at ? new Date(health.observed_at).toLocaleString(ru ? "ru-RU" : "en-US") : "—"}</td></>;
}

function StatusGroup({ language, module, unavailable }: { language: Language; module: Module; unavailable: boolean }) {
  const [expanded, setExpanded] = useState(false);
  const components = module.components ?? [];
  const rowId = (id: string) => `status-${module.module_id}-${id}`;
  return <tbody data-module-id={module.module_id}>
    <tr><td>{components.length ? <button className="status-toggle" aria-expanded={expanded} aria-controls={components.map(component => rowId(component.id)).join(" ")} onClick={() => setExpanded(value => !value)}><span aria-hidden="true" className="status-toggle-mark">{expanded ? "▾" : "▸"}</span>{module.title[language]}</button> : module.title[language]}</td><StatusCells language={language} health={module.status} unavailable={unavailable} /><OpenLink language={language} module={module} links={module} title={module.title} /></tr>
    {components.map((component, index) => {
      const health: Health = module.status.components?.find(report => report.id === component.id) ?? { state: "UNKNOWN", observed_at: null };
      return <tr key={component.id} id={rowId(component.id)} className="status-component" hidden={!expanded}><td><span className="status-branch" aria-hidden="true">{index === components.length - 1 ? "└─" : "├─"}</span>{component.title[language]}</td><StatusCells language={language} health={{ ...health, connection: module.status.connection }} unavailable={unavailable} /><OpenLink language={language} module={module} links={component} title={component.title} /></tr>;
    })}
  </tbody>;
}

export function SystemStatusPanel({ language, modules, unavailable, loading }: { language: Language; modules: Module[]; unavailable: boolean; loading: boolean }) {
  const ru = language === "ru";
  return <section className="aelev-panel" aria-live="polite">{unavailable && <p role="alert">{ru ? "Не удалось обновить состояние. Доступность сервисов неизвестна." : "Status refresh failed. Service availability is unknown."}</p>}{loading ? <p>{ru ? "Загрузка…" : "Loading…"}</p> : !modules.length ? <div className="empty-state"><span className="empty-mark">↗</span><h2>{ru ? "Пространство готово к подключению" : "Ready for services"}</h2><p className="aelev-muted">{ru ? "После публикации описания владельцем здесь появятся его название, страницы и состояние." : "A published description adds its owner's name, pages and status here."}</p></div> : <table><thead><tr><th>{ru ? "Сервис" : "Service"}</th><th>{ru ? "Состояние" : "Status"}</th><th>{ru ? "Последнее сообщение" : "Last report"}</th><th>{ru ? "Интерфейс" : "Interface"}</th></tr></thead>{modules.map(module => <StatusGroup key={module.module_id} language={language} module={module} unavailable={unavailable} />)}</table>}</section>;
}
