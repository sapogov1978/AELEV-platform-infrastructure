export type Language = "ru" | "en";
export type Title = Record<Language, string>;
export type Section = { id: string; title: Title; url: string };
export type Health = { state: "ONLINE" | "OFFLINE" | "UNKNOWN"; reported_state?: "ONLINE" | "OFFLINE" | "UNKNOWN"; observed_at: string | null; connection?: "CONNECTED" | "LOST"; received_at?: string | null };
export type ExternalLinks = { ui_url?: string | null; swagger_url?: string | null };
export type Component = ExternalLinks & { id: string; title: Title };
export type ComponentStatus = Health & { id: string };
export type Module = ExternalLinks & { schema_version: 1; module_id: string; revision: number; title: Title; sections: Section[]; service_kind?: "FUNCTIONAL" | "INFRASTRUCTURE" | null; navigation_order?: number | null; components?: Component[] | null; status: Health & { sequence: number; components?: ComponentStatus[] | null } };
export type Config = { keycloak_url: string; realm: string; client_id: string; poll_seconds: number; stylesheet_sha256: string };

export function parseConfig(value: unknown): Config {
  if (!value || typeof value !== "object") throw new Error("Отсутствует config.json");
  const cfg = value as Record<string, unknown>;
  if (Object.keys(cfg).sort().join() !== "client_id,keycloak_url,poll_seconds,realm,stylesheet_sha256" || typeof cfg.keycloak_url !== "string" || typeof cfg.realm !== "string" || !cfg.realm || typeof cfg.client_id !== "string" || !cfg.client_id || typeof cfg.poll_seconds !== "number" || !Number.isInteger(cfg.poll_seconds) || cfg.poll_seconds <= 0 || typeof cfg.stylesheet_sha256 !== "string" || !/^[a-f0-9]{64}$/.test(cfg.stylesheet_sha256)) throw new Error("Некорректный config.json");
  const url = new URL(cfg.keycloak_url);
  if (url.protocol !== "https:" || url.username || url.password || url.search || url.hash || url.pathname !== "/") throw new Error("Требуется HTTPS origin входа");
  return cfg as Config;
}

export function presentationMessage(event: MessageEvent, frame: Window | null, url: string, language: Language, digest: string): boolean {
  if (!frame || event.source !== frame || event.origin !== new URL(url).origin || !event.data || event.data.type !== "AELEV_UI_READY" || event.data.version !== 1) return false;
  frame.postMessage({ type: "AELEV_UI_CONTEXT", version: 1, language, stylesheet_url: location.origin + "/presentation/v6.css", stylesheet_sha256: digest }, event.origin);
  return true;
}
