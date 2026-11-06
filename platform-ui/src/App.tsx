import { useEffect, useRef, useState } from "react";
import type Keycloak from "keycloak-js";
import type { Config, Language, Module } from "./types";
import { presentationMessage } from "./types";
import { SystemStatusPanel } from "./SystemStatusPanel";
import { ServiceNavigation, type Selection } from "./ServiceNavigation";
import { DataRefreshIndicator } from "./DataRefreshIndicator";
import { ownerRefreshMessage, reportSummary, UNKNOWN_REFRESH, type RefreshState } from "./dataRefresh";
import "./App.css";

export function App({ keycloak, config }: { keycloak: Keycloak; config: Config }) {
  const [language, setLanguage] = useState<Language>("ru");
  const [modules, setModules] = useState<Module[]>([]);
  const [selection, setSelection] = useState<Selection>(null);
  const [error, setError] = useState(false);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(true);
  const [updatedAt, setUpdatedAt] = useState<number | null>(null);
  const [ownerRefresh, setOwnerRefresh] = useState<{ source: string; state: RefreshState } | null>(null);
  const frame = useRef<HTMLIFrameElement>(null);
  const ru = language === "ru";
  const selected = modules.find(item => item.module_id === selection?.module && item.service_kind === "FUNCTIONAL");
  const section = selected?.sections.find(item => item.id === selection?.section);
  const source = section && new URL(section.url).origin !== location.origin ? `${selected?.module_id}/${section.id}/${section.url}` : null;
  const refreshState = source ? (ownerRefresh?.source === source ? ownerRefresh.state : UNKNOWN_REFRESH) : { refreshing, failed: error, updatedAt, pollSeconds: config.poll_seconds };

  useEffect(() => { setOwnerRefresh(null); }, [source]);
  useEffect(() => { if (selection && !section) setSelection(null); }, [selection, section]);

  function frameLoaded() {
    setOwnerRefresh(null);
    if (section) frame.current?.contentWindow?.postMessage({ type: "AELEV_UI_CONTEXT", version: 1, language, stylesheet_url: location.origin + "/presentation/v6.css", stylesheet_sha256: config.stylesheet_sha256 }, new URL(section.url).origin);
  }

  useEffect(() => {
    let disposed = false;
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    const refresh = async () => {
      if (!disposed) setRefreshing(true);
      try {
        await keycloak.updateToken(30);
        if (!keycloak.token) throw new Error("missing access token");
        const response = await fetch("/api/modules", { headers: { Authorization: "Bearer " + keycloak.token }, cache: "no-store", signal: controller.signal });
        if (!response.ok) throw new Error("metadata unavailable");
        const data = await response.json() as { modules: Module[] };
        if (!Array.isArray(data.modules)) throw new Error("invalid metadata");
        if (!disposed) { setModules(data.modules); setError(false); setUpdatedAt(Date.now()); }
      } catch { if (!disposed) setError(true); }
      finally { if (!disposed) { setLoading(false); setRefreshing(false); timer = setTimeout(() => void refresh(), config.poll_seconds * 1000); } }
    };
    void refresh();
    return () => { disposed = true; controller.abort(); clearTimeout(timer); };
  }, [keycloak, config.poll_seconds]);

  useEffect(() => {
    if (!section || !source) return;
    const listener = (event: MessageEvent) => {
      const current = frame.current?.contentWindow ?? null;
      presentationMessage(event, current, section.url, language, config.stylesheet_sha256);
      setOwnerRefresh(previous => {
        const next = ownerRefreshMessage(event, current, section.url, previous?.source === source ? previous.state : UNKNOWN_REFRESH);
        return next ? { source, state: next } : previous;
      });
    };
    window.addEventListener("message", listener);
    const target = frame.current?.contentWindow;
    // A loaded owner receives language changes without receiving credentials.
    if (target) target.postMessage({ type: "AELEV_UI_CONTEXT", version: 1, language, stylesheet_url: location.origin + "/presentation/v6.css", stylesheet_sha256: config.stylesheet_sha256 }, new URL(section.url).origin);
    return () => window.removeEventListener("message", listener);
  }, [section, source, language, config.stylesheet_sha256]);

  return <main className="app-shell">
    <header className="app-header"><button className="brand" onClick={() => setSelection(null)}><span className="brand-mark">D</span><span><strong>DRP</strong><small>Discovery Research Platform</small></span></button><nav className="utility-nav"><DataRefreshIndicator language={language} sourceLabel={source ? selected!.title[language] : (ru ? "Состояние сервисов" : "Service status")} pollSeconds={refreshState.pollSeconds} refreshing={refreshState.refreshing} failed={refreshState.failed} updatedAt={refreshState.updatedAt} reports={source ? undefined : reportSummary(modules)} /><button onClick={() => setSelection(null)}>{ru ? "Состояние" : "Status"}</button><button onClick={() => setLanguage(ru ? "en" : "ru")}>{ru ? "EN" : "RU"}</button><button onClick={() => void keycloak.logout({ redirectUri: location.origin + "/" })}>{ru ? "Выход" : "Sign out"}</button></nav></header>
    <div className="workspace"><ServiceNavigation language={language} modules={modules} selection={section ? selection : null} onSelect={setSelection} />
      <article className="content">{section && new URL(section.url).origin !== location.origin ? <iframe key={selected?.module_id + section.id + section.url} ref={frame} onLoad={frameLoaded} className="owner-frame" title={section.title[language]} src={section.url} sandbox="allow-scripts allow-forms allow-same-origin allow-popups allow-popups-to-escape-sandbox" referrerPolicy="no-referrer" /> : <><div className="page-intro"><p className="eyebrow">DRP</p><h1>{ru ? "Состояние сервисов" : "Service status"}</h1><p className="aelev-muted">{ru ? "Названия, разделы и состояние предоставляют сами сервисы." : "Services supply their names, sections and status."}</p></div><SystemStatusPanel language={language} modules={modules} unavailable={error} loading={loading} /></>}</article>
    </div>
  </main>;
}
