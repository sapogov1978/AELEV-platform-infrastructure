import { useCallback, useEffect, useRef, useState } from "react";
import type Keycloak from "keycloak-js";
import { type QueuePresentation, type QueueInspection } from "./OwnedQueuePanel";
import { ProcessControlPanel } from "./ProcessControlPanel";
import { ServiceNavigation, useGuardedNavigation } from "./ServiceNavigation";
import { ServiceSurfaceFrame } from "./ServiceSurfaceFrame";
import { embedded, stateView, useModuleLanguage, reportModuleDirty } from "./moduleRuntime";
import { POLL_INTERVAL_MS, useSerialRefresh } from "./polling";
import type { Language } from "./modules";
import { QueueResultHero, type QueueProgress } from "./QueueResultHero";
import { QueueCurrentState, type QueueOutcomes, type QueueRoutes } from "./QueueCurrentState";

type View = "overview" | "activity" | "decisions" | "policy" | "candidates";
export function activitySurface(url: string, inspection?: QueueInspection): string {
  const result = new URL(url);
  const stage = result.searchParams.get("filter_stage");
  const selection = inspection?.selection;
  const category = selection === "rejected" ? "rejected" : selection === "accepted" ? "builder" : selection === "uncertain" ? stage === "MINILM" ? "operator" : "expert" : "queue";
  result.searchParams.set("category", category);
  return result.href;
}
export type QueueSummary = { total: number; completed: number; pending: number; running: number; failed: number; cancelled?: number; cancellationDetails?: { after: number; before: number }; outcomes?: QueueOutcomes; outcomeDetails?: { historicalResearch: number; missing: number; unsupported: number }; oldest: string | null; progress?: QueueProgress; activity?: { completed: number; progress: QueueProgress }; raw: unknown };

function Overview({ language, name, title, routes, load, onActivity, pollIntervalMs }: { language: Language; name: string; title: string; routes: QueueRoutes; load: () => Promise<QueueSummary>; onActivity: (inspection: QueueInspection) => void; pollIntervalMs: number }) {
  const [data, setData] = useState<QueueSummary>();
  const [error, setError] = useState("");
  const ru = language === "ru";
  const refresh = useSerialRefresh(useCallback(async () => {
    try { setData(await load()); setError(""); } catch (failure) { setError(String(failure)); }
  }, [load]));
  useEffect(() => { void refresh(); const timer = window.setInterval(() => void refresh(), pollIntervalMs); return () => clearInterval(timer); }, [pollIntervalMs, refresh]);
  return <section className="overview">
    {error && <p className="error" role="alert">{error}</p>}
    <div className="overview-heading"><div><span className="section-kicker">{ru ? "Работа фильтра" : "Filter activity"}</span><h2>{ru ? "Как работает эта стадия" : "How this stage is running"}</h2><p>{ru ? "Счётчики собственной очереди и выполненных заданий." : "This stage's queue and completed-job counters."}</p></div><button className="quiet compact" onClick={() => void refresh()}>{ru ? "Обновить обзор" : "Refresh overview"}</button></div>
    {!data ? <p className="empty">{ru ? "Получаем счётчики сервиса…" : "Loading service counters…"}</p> : <>
      <QueueResultHero language={language} name={name} total={data.total} completed={data.activity?.completed ?? data.completed} remaining={data.pending + data.running} failed={data.failed} progress={data.activity?.progress ?? data.progress} includesCancelledCompletions={Boolean(data.activity)} onActivity={onActivity} />
      <QueueCurrentState language={language} title={title} data={data} routes={routes} onActivity={onActivity} />
    </>}
  </section>;
}

/** Shared layout only: each app supplies its owner-local reads and exact owner surfaces. */
export function QueueServiceApp({ keycloak, name, title, description, routes, summary, panels, roleNames, pollIntervalMs = POLL_INTERVAL_MS }: {
  keycloak: Keycloak; name: string; title: Record<Language, string>; description: Record<Language, string>;
  routes: QueueRoutes;
  load: (offset: number, activeOnly: boolean, inspection?: QueueInspection) => Promise<QueuePresentation>; summary: () => Promise<QueueSummary>;
  details: (id: string) => Promise<unknown>; evidence: (id: string) => Promise<unknown>; evidenceText: (value: unknown) => string | undefined;
  detailFacts?: (value: unknown, language: Language) => { label: string; value: string }[];
  panels: { activity: string; model: string; operator: string; policy: string; candidates?: string }; roleNames: Record<string, string>; pollIntervalMs?: number;
}) {
  const [language, setLanguage] = useModuleLanguage();
  const [state, setState] = useState(stateView);
  const [view, setView] = useState<View>("overview");
  const [inspection, setInspection] = useState<QueueInspection>({ selection: "active" });
  const [operator, setOperator] = useState(false);
  const dirty = useRef(false);
  const ru = language === "ru";
  const onDirty = useCallback((value: boolean) => { dirty.current = value; reportModuleDirty(value); }, []);
  const { navigate, confirmation } = useGuardedNavigation({ language, isDirty: () => dirty.current, onDiscard: () => onDirty(false) });
  const items: { id: View; label: string }[] = [
    { id: "overview", label: ru ? "Обзор" : "Overview" }, { id: "activity", label: ru ? "Работа сервиса" : "Service activity" },
    { id: "decisions", label: ru ? "Решения" : "Decisions" }, { id: "policy", label: ru ? "Политика" : "Policy" },
    ...(panels.candidates ? [{ id: "candidates" as const, label: ru ? "Кандидаты, передаваемые в билдер" : "Candidates sent to Builder" }] : []),
  ];
  const selectedPanel = view === "activity" ? activitySurface(panels.activity, inspection) : view === "policy" ? panels.policy : view === "candidates" ? panels.candidates : view === "decisions" ? operator ? panels.operator : panels.model : undefined;
  const panelTitle = view === "decisions" ? operator ? (ru ? "Решения оператора" : "Operator decisions") : (ru ? "Решения модели" : "Model decisions") : items.find(item => item.id === view)?.label ?? "";
  return <main className="app-shell filter-app aelev-owner-ui">
    {!embedded && <header className="app-header"><div className="app-brand"><span className="brand-mark">D</span><div><strong>DRP</strong><span>Discovery Research Platform</span></div></div><nav className="utility-nav"><button className="quiet compact" onClick={() => navigate(() => setState(!state))}>{state ? title[language] : (ru ? "Состояние" : "State")}</button><button className="quiet compact" onClick={() => setLanguage(ru ? "en" : "ru")}>{ru ? "EN" : "RU"}</button><button className="quiet compact" onClick={() => navigate(() => void keycloak.logout({ redirectUri: location.origin }))}>{ru ? "Выход" : "Sign out"}</button></nav></header>}
    {(!embedded || !state) && <section className="page-intro"><div><span className="section-kicker">{name}</span><h1>{title[language]}</h1><p>{description[language]}</p></div></section>}
    {confirmation && <div className="filter-content navigation-confirmation-area">{confirmation}</div>}
    {state ? <ProcessControlPanel delivery keycloak={keycloak} apiUrl="/api" language={language} roleNames={roleNames} pollIntervalMs={pollIntervalMs} /> : <>
      <ServiceNavigation items={items} selected={view} onSelect={value => navigate(() => { if (value === "activity") setInspection({ selection: "active" }); setView(value); })} label={ru ? "Разделы фильтра" : "Filter sections"} />
      <div className="filter-content">
      {view === "overview" && <Overview language={language} name={name} title={title[language]} routes={routes} load={summary} onActivity={value => { setInspection(value); setView("activity"); }} pollIntervalMs={pollIntervalMs} />}
      {view === "decisions" && <div className="decision-navigation"><button className={!operator ? "active" : ""} onClick={() => navigate(() => setOperator(false))}>{ru ? "Решения модели" : "Model decisions"}</button><button className={operator ? "active" : ""} onClick={() => navigate(() => setOperator(true))}>{ru ? "Решения оператора" : "Operator decisions"}</button></div>}
      {selectedPanel && <ServiceSurfaceFrame key={selectedPanel} url={selectedPanel} title={panelTitle} language={language} onDirty={onDirty} />}
      </div>
    </>}
  </main>;
}
