import React from "react";
import ReactDOM from "react-dom/client";
import Keycloak from "keycloak-js";
import { App } from "./App";
import { parseConfig } from "./types";

const root = ReactDOM.createRoot(document.getElementById("root")!);
root.render(<div className="boot">Подключение…</div>);

async function boot() {
  if (window.top !== window.self) throw new Error("Оболочка не допускает встраивания");
  const response = await fetch("/config.json", { cache: "no-store" });
  if (!response.ok) throw new Error("Отсутствует config.json");
  const config = parseConfig(await response.json());
  const keycloak = new Keycloak({ url: config.keycloak_url, realm: config.realm, clientId: config.client_id });
  await keycloak.init({ onLoad: "login-required", pkceMethod: "S256", checkLoginIframe: false });
  root.render(<React.StrictMode><App keycloak={keycloak} config={config} /></React.StrictMode>);
}
void boot().catch(() => root.render(<div className="fatal">Не удалось загрузить настройки или выполнить вход. Проверьте HTTPS и конфигурацию.</div>));
