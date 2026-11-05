import type Keycloak from "keycloak-js";

export const SILENT_SSO_TIMEOUT_MS = 10_000;

export function authorizationCallback(data: unknown, origin: string, state: string): URL | undefined {
  if (!state || typeof data !== "string" || data.length > 8192) return;
  let callback: URL;
  try { callback = new URL(data); } catch { return; }
  const values = new URLSearchParams(callback.hash.slice(1));
  if (callback.origin !== origin || callback.username || callback.password || callback.pathname !== "/auth-callback.html"
    || values.get("state") !== state || (!values.get("code") && !values.get("error"))
    || values.has("access_token") || values.has("refresh_token")) return;
  return callback;
}

/** Obtain only an authorization callback; the owning SDK still verifies PKCE and nonce. */
export async function trySilentSso(client: Pick<Keycloak, "createLoginUrl">, origin: string): Promise<string | undefined> {
  const loginUrl = await client.createLoginUrl({ prompt: "none", redirectUri: `${origin}/auth-callback.html` });
  const state = new URL(loginUrl).searchParams.get("state");
  if (!state) return;
  return new Promise(resolve => {
    const frame = document.createElement("iframe");
    frame.hidden = true;
    frame.title = "service-silent-sso";
    frame.setAttribute("sandbox", "allow-scripts allow-same-origin");
    frame.src = loginUrl;
    let finished = false;
    let timeout: number | undefined;
    const finish = (hash?: string) => {
      if (finished) return;
      finished = true;
      if (timeout !== undefined) window.clearTimeout(timeout);
      window.removeEventListener("message", receive);
      window.removeEventListener("pagehide", cancel);
      frame.remove();
      resolve(hash);
    };
    const receive = (event: MessageEvent) => {
      if (event.source !== frame.contentWindow || event.origin !== origin) return;
      const callback = authorizationCallback(event.data, origin, state);
      if (callback) finish(new URLSearchParams(callback.hash.slice(1)).has("error") ? undefined : callback.hash);
    };
    const cancel = () => finish();
    window.addEventListener("message", receive);
    window.addEventListener("pagehide", cancel);
    timeout = window.setTimeout(cancel, SILENT_SSO_TIMEOUT_MS);
    try { document.body.appendChild(frame); } catch { finish(); }
  });
}
