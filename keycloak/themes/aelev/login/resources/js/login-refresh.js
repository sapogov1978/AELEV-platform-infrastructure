/* Refresh the server-owned login action before the first credential POST. */
(() => {
  "use strict";

  function install() {
    const form = document.getElementById("kc-form-login");
    // Do not restart password-only, OTP, passkey or required-action steps.
    if (!form || !form.querySelector('input[name="username"]') ||
        !form.querySelector('input[name="password"][type="password"]')) return;

    const realmPath = location.pathname.match(/^(.*\/realms\/[^/]+)\//)?.[1];
    if (!realmPath) return;
    const actionPath = `${realmPath}/login-actions/authenticate`;
    function loginAction(value) {
      const url = new URL(value, location.href);
      if (url.origin !== location.origin || url.pathname !== actionPath ||
          url.username || url.password || !url.searchParams.get("client_id")) {
        throw new Error("Unexpected login action");
      }
      return url;
    }

    // Keep the original authorization request, not an expired login action.
    // The per-tab record is bound to the server's exact client context. Never
    // reconstruct OIDC parameters from client_data or store credential fields.
    const authorizationPath = `${realmPath}/protocol/openid-connect/auth`;
    const storageKey = `aelev.login-request:${realmPath}`;
    let authorization;
    try {
      const action = loginAction(form.action);
      const validAuthorization = (value) => {
        const url = new URL(value);
        if (url.origin !== location.origin || url.pathname !== authorizationPath ||
            url.username || url.password || url.hash || url.href.length > 16384 ||
            url.searchParams.getAll("client_id").length !== 1 ||
            url.searchParams.get("client_id") !== action.searchParams.get("client_id") ||
            url.searchParams.get("response_type") !== "code" ||
            !url.searchParams.get("redirect_uri")) throw new Error("Unexpected authorization request");
        return url.href;
      };
      if (location.pathname === authorizationPath) {
        authorization = validAuthorization(location.href);
        try {
          sessionStorage.setItem(storageKey, JSON.stringify({
            href: authorization,
            clientData: action.searchParams.get("client_data"),
          }));
        } catch { /* Storage may be disabled; this page still has its request. */ }
      } else {
        const saved = JSON.parse(sessionStorage.getItem(storageKey));
        if (saved && saved.clientData === action.searchParams.get("client_data")) {
          authorization = validAuthorization(saved.href);
        }
      }
    } catch { /* Missing or foreign context must not authorize a restart. */ }

    let busy = false;
    let ready = false;
    let submitted = false;
    let leaving = false;
    let controller;
    let submittedButton;
    const message = document.createElement("p");
    message.setAttribute("role", "alert");
    message.hidden = true;
    form.prepend(message);
    function leave() {
      leaving = true;
      controller?.abort();
    }
    window.addEventListener("pagehide", leave);
    window.addEventListener("beforeunload", leave);
    window.addEventListener("pageshow", () => {
      leaving = false;
      submitted = false;
      if (submittedButton) submittedButton.disabled = false;
      submittedButton = undefined;
    });

    form.addEventListener("submit", async (event) => {
      if (ready) {
        ready = false;
        submitted = true;
        submittedButton = form.querySelector("#kc-login") || event.submitter;
        return;
      }
      event.preventDefault();
      if (busy || leaving || submitted) return;
      busy = true;
      message.hidden = true;
      const submitter = event.submitter;
      const button = form.querySelector("#kc-login") || submitter;
      if (button) button.disabled = true;
      controller = new AbortController();
      const timeout = setTimeout(() => controller.abort(), 10000);
      try {
        if (form.method.toLowerCase() !== "post" ||
            submitter?.hasAttribute("formaction") || submitter?.hasAttribute("formmethod")) {
          throw new Error("Unexpected submission method");
        }
        const previous = loginAction(form.action);
        const check = new URL(previous);
        // A GET without a one-use action code invokes Keycloak's normal
        // expiry/restart checks, without sending or reading any credentials.
        check.searchParams.delete("session_code");
        const options = {
          method: "GET",
          credentials: "same-origin",
          mode: "same-origin",
          cache: "no-store",
          referrerPolicy: "same-origin",
          signal: controller.signal,
        };
        let response = await fetch(check, options);
        // Keycloak returns 400 when neither the authentication session nor its
        // signed restart cookie survives. A normal OIDC request creates a new
        // server-owned session with the original state, nonce and PKCE intact.
        // Only one recovery GET is allowed; network/5xx/MFA errors never do this.
        if (response.status === 400 && authorization &&
            new URL(response.url).origin === location.origin &&
            new URL(response.url).pathname === actionPath &&
            response.headers.get("content-type")?.includes("text/html")) {
          await response.body?.cancel();
          response = await fetch(authorization, options);
        }
        if (!response.ok || new URL(response.url).origin !== location.origin ||
            !response.headers.get("content-type")?.includes("text/html")) {
          throw new Error("Login refresh unavailable");
        }
        const html = await response.text();
        if (html.length > 524288) throw new Error("Login response too large");
        const page = new DOMParser().parseFromString(html, "text/html");
        const fresh = page.getElementById("kc-form-login");
        if (!fresh || fresh.method.toLowerCase() !== "post" ||
            !fresh.querySelector('input[name="username"]') ||
            !fresh.querySelector('input[name="password"][type="password"]')) {
          throw new Error("Login flow changed");
        }
        const next = loginAction(fresh.getAttribute("action"));
        for (const parameter of ["client_id", "client_data"]) {
          if (next.searchParams.get(parameter) !== previous.searchParams.get(parameter)) {
            throw new Error("Login context changed");
          }
        }
        if (!next.searchParams.get("session_code")) throw new Error("Missing action code");
        // Hidden credential selection must not be transferred across flows.
        const hidden = Array.from(form.querySelectorAll('input[type="hidden"]'));
        const freshHidden = Array.from(fresh.querySelectorAll('input[type="hidden"]'));
        if (hidden.length !== freshHidden.length) throw new Error("Login fields changed");
        for (const current of hidden) {
          const replacement = freshHidden
            .find((input) => input.name === current.name);
          if (!replacement || replacement.value !== current.value) {
            throw new Error("Login challenge changed");
          }
        }
        if (leaving || !form.isConnected) return;
        form.action = next.href;
        if (button) button.disabled = false;
        ready = true;
        // Native submission preserves validation, submitter and browser-only
        // credential fields. Never fetch a password or retry a credential POST.
        form.requestSubmit(submitter || undefined);
        ready = false;
      } catch {
        if (!leaving) {
          message.textContent = document.documentElement.lang.startsWith("ru")
            ? "Не удалось обновить попытку входа. Проверьте соединение и нажмите «Войти» ещё раз. Если ошибка повторяется, откройте приложение заново."
            : "Could not refresh sign-in. Check your connection and select Sign in again. If this persists, reopen the application.";
          message.hidden = false;
        }
      } finally {
        ready = false;
        clearTimeout(timeout);
        controller = undefined;
        busy = false;
        if (button && !submitted) button.disabled = false;
      }
    }, { capture: true });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", install, { once: true });
  } else {
    install();
  }
})();
