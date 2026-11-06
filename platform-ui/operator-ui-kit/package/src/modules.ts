export type Language = "ru" | "en";
export interface UiManifest {
  schema_version: "1.0.0";
  module_id: string;
  release: string;
  title: Record<Language, string>;
  entry_path: string;
  state_path?: string;
}
export interface UiModule {
  manifest: UiManifest;
  origin: string;
  image_digest: string;
}
export interface UiCatalogue {
  schema_version: "1.0.0";
  generation: string;
  observed_at: string;
  expires_at: string;
  source_status: "CURRENT" | "UNKNOWN";
  modules: UiModule[];
}
export type ModuleMessage = {
  protocol_version: "1";
  session_id: string;
} & ({ type: "CONTEXT"; value: Language } | { type: "HEIGHT"; value: number } | { type: "DIRTY"; value: boolean });

function record(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}
function boundedString(value: unknown, limit = 200): value is string {
  return typeof value === "string" && value.length > 0 && value.length <= limit;
}
function onlyKeys(value: Record<string, unknown>, keys: string[]): boolean {
  return Object.keys(value).every(key => keys.includes(key));
}
function safePath(value: unknown): value is string {
  return boundedString(value, 1024) && value.startsWith("/") && !value.startsWith("//")
    && !/[\\\u0000-\u0020]/.test(value);
}
export function parseCatalogue(value: unknown, shellOrigin: string): UiCatalogue {
  if (!record(value) || !onlyKeys(value, ["schema_version", "generation", "observed_at", "expires_at", "source_status", "modules"])
    || value.schema_version !== "1.0.0" || !boundedString(value.generation)
    || !Array.isArray(value.modules) || value.modules.length > 256
    || typeof value.source_status !== "string" || !["CURRENT", "UNKNOWN"].includes(value.source_status)
    || typeof value.observed_at !== "string" || typeof value.expires_at !== "string"
    || !Number.isFinite(Date.parse(value.observed_at)) || !Number.isFinite(Date.parse(value.expires_at))
    || Date.parse(value.expires_at) <= Date.parse(value.observed_at)) throw new Error("Invalid UI catalogue");
  const ids = new Set<string>();
  for (const item of value.modules) {
    if (!record(item) || !onlyKeys(item, ["manifest", "origin", "image_digest"]) || typeof item.origin !== "string" || typeof item.image_digest !== "string"
      || !/^sha256:[a-f0-9]{64}$/.test(item.image_digest) || !record(item.manifest)) throw new Error("Invalid UI module");
    const url = new URL(item.origin);
    if (!["http:", "https:"].includes(url.protocol) || url.origin !== item.origin || url.origin === shellOrigin
      || url.username || url.password) throw new Error("Invalid UI origin");
    const m = item.manifest;
    if (!onlyKeys(m, ["schema_version", "module_id", "release", "title", "entry_path", "state_path"])
      || m.schema_version !== "1.0.0" || !boundedString(m.module_id) || !/^[a-z0-9][a-z0-9-]*$/.test(m.module_id)
      || ids.has(m.module_id) || !boundedString(m.release) || !record(m.title)
      || !onlyKeys(m.title, ["ru", "en"])
      || !boundedString(m.title.ru) || !boundedString(m.title.en) || !safePath(m.entry_path)
      || (m.state_path !== undefined && !safePath(m.state_path))) throw new Error("Invalid UI manifest");
    if (new URL(m.entry_path, url).origin !== url.origin
      || (m.state_path && new URL(m.state_path, url).origin !== url.origin)) throw new Error("Invalid UI path");
    ids.add(m.module_id);
  }
  return value as unknown as UiCatalogue;
}
export function catalogueIsCurrent(catalogue: UiCatalogue, now = Date.now()): boolean {
  return catalogue.source_status === "CURRENT" && Date.parse(catalogue.observed_at) <= now + 30_000
    && Date.parse(catalogue.expires_at) > now;
}
export function validModuleMessage(value: unknown, sessionId: string): value is ModuleMessage {
  if (!record(value) || value.protocol_version !== "1" || value.session_id !== sessionId
    || Object.keys(value).some(key => !["protocol_version", "session_id", "type", "value"].includes(key))) return false;
  try { if (JSON.stringify(value).length > 4096) return false; } catch { return false; }
  return value.type === "CONTEXT" ? typeof value.value === "string" && ["ru", "en"].includes(value.value)
    : value.type === "DIRTY" ? typeof value.value === "boolean"
    : value.type === "HEIGHT" && typeof value.value === "number" && Number.isFinite(value.value)
      && value.value >= 320 && value.value <= 1600;
}

export async function readCatalogue(response: Response, shellOrigin: string): Promise<UiCatalogue> {
  if (!response.ok || !response.body) throw new Error(`UI catalogue HTTP ${response.status}`);
  const reader = response.body.getReader();
  const chunks: Uint8Array[] = [];
  let size = 0;
  try {
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      size += value.byteLength;
      if (size > 256 * 1024) throw new Error("UI catalogue size limit exceeded");
      chunks.push(value);
    }
  } finally { await reader.cancel(); reader.releaseLock(); }
  const bytes = new Uint8Array(size);
  let offset = 0;
  for (const chunk of chunks) { bytes.set(chunk, offset); offset += chunk.byteLength; }
  return parseCatalogue(JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(bytes)), shellOrigin);
}
