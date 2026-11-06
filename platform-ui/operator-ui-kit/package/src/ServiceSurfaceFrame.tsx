import { useEffect, useRef, useState } from "react";
import { validModuleMessage, type Language } from "./modules";

export function surfaceUrl(value: string, hostOrigin: string, sessionId: string, language: Language): string {
  const url = new URL(value);
  if (!["http:", "https:"].includes(url.protocol) || url.username || url.password) throw new Error("Invalid service UI URL");
  url.searchParams.set("embedded", "1");
  url.searchParams.set("host_origin", hostOrigin);
  url.searchParams.set("session_id", sessionId);
  url.searchParams.set("language", language);
  return url.href;
}

/** Isolated owner UI: only technical context, size and edit state cross the frame. */
export function ServiceSurfaceFrame({ url, title, language, onDirty }: {
  url: string; title: string; language: Language; onDirty: (dirty: boolean) => void;
}) {
  const frame = useRef<HTMLIFrameElement>(null);
  const [sessionId] = useState(() => crypto.randomUUID());
  const [height, setHeight] = useState(1000);
  const [src] = useState(() => surfaceUrl(url, location.origin, sessionId, language));
  const origin = new URL(src).origin;
  const context = () => frame.current?.contentWindow?.postMessage({ protocol_version: "1", session_id: sessionId, type: "CONTEXT", value: language }, origin);
  useEffect(() => {
    context();
    const receive = (event: MessageEvent) => {
      if (event.origin !== origin || event.source !== frame.current?.contentWindow || !validModuleMessage(event.data, sessionId)) return;
      if (event.data.type === "HEIGHT") { setHeight(event.data.value); context(); }
      if (event.data.type === "DIRTY") onDirty(event.data.value);
    };
    window.addEventListener("message", receive);
    return () => window.removeEventListener("message", receive);
  }, [origin, sessionId, language, onDirty]);
  return <section className="module-frame service-surface">
    <div className="panel-heading"><h2>{title}</h2><a href={url} target="_blank" rel="noopener noreferrer">{language === "ru" ? "Открыть отдельно" : "Open standalone"}</a></div>
    <iframe ref={frame} src={src} title={title} style={{ height }} onLoad={context}
      sandbox="allow-scripts allow-same-origin allow-forms allow-popups allow-popups-to-escape-sandbox" referrerPolicy="no-referrer" />
  </section>;
}
