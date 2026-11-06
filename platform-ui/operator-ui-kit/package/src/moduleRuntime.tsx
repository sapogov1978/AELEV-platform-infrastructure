import { useEffect, useState, type ReactNode } from "react";
import { createRoot } from "react-dom/client";
import Keycloak from "keycloak-js";
import { validModuleMessage, type Language } from "./modules";
import { authorizationCallback, trySilentSso } from "./moduleAuth";

const parameters = new URLSearchParams(typeof location === "undefined" ? "" : location.search);
export const stateView = parameters.get("view") === "state";
const sessionId = parameters.get("session_id") ?? "";
const allowedHosts = (import.meta.env.VITE_EMBEDDING_ORIGINS ?? "").split(" ").filter(Boolean);
const hostOrigin = parameters.get("host_origin") ?? "";
const connected = parameters.get("embedded") === "1" && window.parent !== window && allowedHosts.includes(hostOrigin) && sessionId.length <= 200 && !!sessionId;
export const embedded = connected;

function report(type: "HEIGHT" | "DIRTY", value: number | boolean) {
  if (connected) window.parent.postMessage({ protocol_version: "1", session_id: sessionId, type, value }, hostOrigin);
}
export function reportModuleDirty(dirty: boolean) { report("DIRTY", dirty); }
export function useModuleLanguage(): [Language, (value: Language) => void] {
  const [language, setLanguage] = useState<Language>(() => new URLSearchParams(typeof location === "undefined" ? "" : location.search).get("language") === "en" ? "en" : "ru");
  useEffect(() => {
    const receive = (event: MessageEvent) => {
      if (connected && event.source === window.parent && event.origin === hostOrigin
        && validModuleMessage(event.data, sessionId) && event.data.type === "CONTEXT") setLanguage(event.data.value);
    };
    window.addEventListener("message", receive);
    return () => window.removeEventListener("message", receive);
  }, []);
  return [language, setLanguage];
}
function connectLayout() {
  if (!connected) return;
  const resize = new ResizeObserver(() => report("HEIGHT", Math.min(1600, Math.max(320, document.body.scrollHeight))));
  resize.observe(document.body);
  const edited = (event: Event) => {
    const target = event.target;
    if (target instanceof HTMLTextAreaElement || target instanceof HTMLSelectElement
      || (target instanceof HTMLInputElement && !["search", "button", "submit"].includes(target.type))) report("DIRTY", true);
  };
  document.addEventListener("input", edited);
  document.addEventListener("change", edited);
  window.addEventListener("pagehide", () => { resize.disconnect(); document.removeEventListener("input", edited); document.removeEventListener("change", edited); }, { once: true });
}

/** Bootstrap a service-owned PKCE session; no token crosses an embedding boundary. */
export async function bootstrapModule(clientId: string, render: (keycloak: Keycloak) => ReactNode, options?: { standaloneRedirectUri?: string }) {
  const root = createRoot(document.getElementById("root")!);
  const keycloak = new Keycloak({
    url: import.meta.env.VITE_KEYCLOAK_URL ?? "http://127.0.0.1:18080",
    realm: import.meta.env.VITE_KEYCLOAK_REALM ?? "aelev",
    clientId,
  });
  root.render(<p className="boot">Подключение приложения…</p>);
  connectLayout();
  try {
    // Each application owns its PKCE session; the embedding host never provides a token.
    const redirectUri = options?.standaloneRedirectUri;
    if (redirectUri && redirectUri !== `${location.origin}/`) throw new Error("Invalid standalone callback");
    await keycloak.init({ ...(embedded ? {} : { onLoad: "login-required" as const, ...(redirectUri ? { redirectUri } : {}) }), pkceMethod: "S256", checkLoginIframe: false });
    if (keycloak.authenticated) { root.render(render(keycloak)); return; }
    if (connected) {
      const callback = await trySilentSso(keycloak, location.origin).catch(() => undefined);
      if (callback) {
        // Reload only the unauthenticated module; its SDK consumes the saved PKCE state.
        location.hash = callback;
        location.reload();
        return;
      }
    }
    let pendingState = "";
    let popup: Window | null = null;
    const callbackUrl = `${location.origin}/auth-callback.html`;
    const login = async () => {
      popup = window.open("about:blank", "_blank", "width=620,height=760");
      if (!popup) throw new Error("Разрешите окно входа для этого приложения");
      const loginUrl = await keycloak.createLoginUrl({ redirectUri: callbackUrl });
      pendingState = new URL(loginUrl).searchParams.get("state") ?? "";
      popup.location.href = loginUrl;
    };
    const receive = (event: MessageEvent) => {
      if (!popup || !pendingState || event.source !== popup || event.origin !== location.origin) return;
      const callback = authorizationCallback(event.data, location.origin, pendingState);
      if (!callback) return;
      window.removeEventListener("message", receive);
      // The SDK verifies saved state, nonce and PKCE on the same application's reload.
      location.hash = callback.hash;
      location.reload();
    };
    window.addEventListener("message", receive);
    window.addEventListener("pagehide", () => { window.removeEventListener("message", receive); popup?.close(); }, { once: true });
    root.render(<section className="panel"><h2>Вход в приложение / Application sign-in</h2><p>Автоматическое подключение недоступно: сессия истекла или браузер заблокировал фоновый вход. Приложение использует собственную сессию; родительский интерфейс не получает его токен.</p><button onClick={() => void login().catch(() => root.render(<p role="alert">Не удалось открыть вход. Откройте приложение отдельно.</p>))}>Подключить / Connect</button><p><a href={location.origin} target="_blank" rel="noopener noreferrer">Открыть самостоятельно / Open standalone</a></p></section>);
  } catch {
    root.render(<p className="fatal" role="alert">Не удалось войти в приложение. Проверьте доступность Keycloak и настройки клиента.</p>);
  }
}
