import React from "react";
import { readFileSync } from "node:fs";
import type Keycloak from "keycloak-js";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";
import { App } from "../src/App";
import { SystemStatusPanel } from "../src/SystemStatusPanel";
import { ServiceNavigation } from "../src/ServiceNavigation";
import { parseConfig, presentationMessage, type Module } from "../src/types";

const module: Module = { schema_version: 1, module_id: "be36a9c0-fbe9-4d14-8b77-3b5fc5c5ca55", revision: 1, title: { ru: "Произвольный участник", en: "Arbitrary owner" }, sections: [], status: { state: "ONLINE", sequence: 1, observed_at: null } };
const participants: Module[] = [
  { ...module, service_kind: "FUNCTIONAL", title: { ru: "Функциональный участник", en: "Functional owner" }, sections: [{ id: "page", title: { ru: "Страница владельца", en: "Owner page" }, url: "https://owner.invalid/page" }] },
  { ...module, module_id: "67dfae64-72d4-48b8-beca-48cc8418223b", service_kind: "INFRASTRUCTURE", title: { ru: "Инфраструктурный участник", en: "Infrastructure owner" } },
  { ...module, module_id: "7cda6f53-45c4-4c81-bd30-e8aff550b06e", title: { ru: "Участник без типа", en: "Unclassified owner" } },
  { ...module, module_id: "8338c99d-6d5d-47d0-a12b-4c460631dcbd", service_kind: null, title: { ru: "Участник с null", en: "Null owner" } },
];
describe("functional service navigation", () => {
  it.each(["ru", "en"] as const)("shows only owner-declared functional services in %s", language => {
    const html = renderToStaticMarkup(React.createElement(ServiceNavigation, { language, modules: participants, selection: null, onSelect: vi.fn() }));
    expect(html).toContain(`>${participants[0].title[language]}</button>`);
    expect(html).not.toContain(participants[0].sections[0].title[language]);
    for (const hidden of participants.slice(1)) expect(html).not.toContain(hidden.title[language]);
    expect(html).not.toContain("Пока не подключены");
    expect(html).not.toContain("Not connected yet");
  });
  it("keeps owner section buttons only when a service declares several sections", () => {
    const grouped = { ...participants[0], sections: [
      participants[0].sections[0],
      { id: "history", title: { ru: "История", en: "History" }, url: "https://owner.invalid/history" },
    ] };
    const html = renderToStaticMarkup(React.createElement(ServiceNavigation, { language: "ru", modules: [grouped], selection: null, onSelect: vi.fn() }));
    expect(html).toContain("Функциональный участник");
    expect(html).toContain("Страница владельца");
    expect(html).toContain("История");
  });
  it("shows an empty navigation even when infrastructure is connected", () => {
    const html = renderToStaticMarkup(React.createElement(ServiceNavigation, { language: "ru", modules: participants.slice(1), selection: null, onSelect: vi.fn() }));
    expect(html).toContain("Пока не подключены");
    expect(html).not.toContain('class="module-nav"');
  });
  it("marks only the selected owner's section", () => {
    const html = renderToStaticMarkup(React.createElement(ServiceNavigation, { language: "en", modules: participants, selection: { module: participants[0].module_id, section: "page" }, onSelect: vi.fn() }));
    expect(html).toContain('aria-pressed="true"');
  });
  it("orders functional services by their owner-declared navigation order", () => {
    const ordinary = { ...participants[0], module_id: "00000000-0000-4000-8000-000000000001", title: { ru: "Обычный", en: "Ordinary" } };
    const last = { ...participants[0], module_id: "00000000-0000-4000-8000-000000000000", navigation_order: 2147483647, title: { ru: "Последний", en: "Last" } };
    const html = renderToStaticMarkup(React.createElement(ServiceNavigation, { language: "ru", modules: [last, ordinary], selection: null, onSelect: vi.fn() }));
    expect(html.indexOf(">Обычный</button>")).toBeLessThan(html.indexOf(">Последний</button>"));
  });
  it("keeps every kind visible in service status", () => {
    const html = renderToStaticMarkup(React.createElement(SystemStatusPanel, { language: "ru", modules: participants, unavailable: false, loading: false }));
    for (const participant of participants) expect(html).toContain(participant.title.ru);
  });
});
describe("platform identity", () => {
  it("uses the Policy Management dark-button palette without styling identity controls as pills", () => {
    const css = readFileSync(new URL("../public/presentation/v6.css", import.meta.url), "utf8");
    expect(css).toContain("--aelev-button-bg: #20523e");
    expect(css).toContain("--aelev-button-border: #6fae91");
    expect(css).toContain("--aelev-button-hover-bg: #2b684f");
    expect(css).toContain("--aelev-button-hover-border: #90d2b4");
    expect(css).toContain(".aelev-owner-ui .panel");
    expect(css).toContain(".aelev-owner-ui .card");
    expect(css).toContain("--aelev-radius: 18px");
    expect(css).toContain("button:not([class]), button.quiet");
    expect(css).toContain(".brand, .status-toggle, .service-navigation button");
    expect(css).toContain(".aelev-owner-ui .decision-navigation > button");
  });
  it("uses the header's rounded mint A as the browser favicon", () => {
    const index = readFileSync(new URL("../index.html", import.meta.url), "utf8");
    const favicon = readFileSync(new URL("../public/favicon.svg", import.meta.url), "utf8");
    const css = readFileSync(new URL("../src/App.css", import.meta.url), "utf8");
    const mark = css.match(/\.brand-mark\{([^}]+)\}/)![1];
    expect(index).toContain('<link rel="icon" type="image/svg+xml" href="/favicon.svg" />');
    expect(favicon).toContain('viewBox="0 0 42 42"');
    expect(favicon).toContain('<rect width="42" height="42" rx="12" fill="#10261f" />');
    expect(favicon).toContain('<path fill="#75e6bf"');
    expect(favicon).not.toMatch(/<text|<script|<image/);
    expect(mark).toContain("width:42px;height:42px");
    expect(mark).toContain("border-radius:12px");
    expect(mark).toContain("background:#10261f;color:#75e6bf");
  });
  it("uses the platform name in the header and browser title", () => {
    const config = parseConfig({ keycloak_url: "https://identity.invalid", realm: "realm", client_id: "shell", poll_seconds: 1, stylesheet_sha256: "a".repeat(64) });
    const html = renderToStaticMarkup(React.createElement(App, { keycloak: {} as Keycloak, config }));
    expect(html).toContain("<strong>DRP</strong><small>Discovery Research Platform</small>");
    expect(html.match(/class="data-refresh data-refresh--/g)).toHaveLength(1);
    expect(html.indexOf('class="data-refresh ')).toBeLessThan(html.indexOf("<button>Состояние</button>"));
    expect(html).toContain('<p class="nav-caption">Подключенные сервисы</p>');
    expect(html).not.toContain("РАЗДЕЛЫ");
    expect(html).not.toContain("Единое пространство");
    expect(readFileSync(new URL("../index.html", import.meta.url), "utf8")).toContain("<title>Discovery Research Platform</title>");
  });
});
describe("owner injection", () => {
  const grouped: Module = { ...module, components: [
    { id: "process", title: { ru: "Процесс владельца", en: "Owner process" } },
    { id: "store", title: { ru: "Хранилище владельца", en: "Owner store" } },
    { id: "reporter", title: { ru: "Издатель владельца", en: "Owner reporter" } },
  ], status: { ...module.status, components: [
    { id: "process", state: "ONLINE", observed_at: "2026-10-10T00:00:00Z" },
    { id: "store", state: "OFFLINE", observed_at: "2026-10-10T00:00:01Z" },
  ] } };
  it.each(["ru", "en"] as const)("renders an owner-defined collapsed group in %s", language => {
    const html = renderToStaticMarkup(React.createElement(SystemStatusPanel, { language, modules: [grouped], unavailable: false, loading: false }));
    expect(html).toContain('aria-expanded="false"');
    expect(html.match(/class="status-component" hidden=""/g)).toHaveLength(3);
    for (const component of grouped.components!) expect(html).toContain(component.title[language]);
    expect(html).toContain('status-pill online');
    expect(html).toContain('status-pill offline');
    expect(html).toContain('status-pill unknown');
    expect(html).toContain(`id="status-${grouped.module_id}-process"`);
  });
  it("does not replace missing component reports with the parent's healthy status", () => {
    const unknown = { ...grouped, status: { ...module.status } };
    const html = renderToStaticMarkup(React.createElement(SystemStatusPanel, { language: "ru", modules: [unknown], unavailable: false, loading: false }));
    expect(html.match(/status-pill unknown/g)).toHaveLength(3);
  });
  it("renders confirmed unavailability as warning and reserves danger for lost connection", () => {
    const connected = renderToStaticMarkup(React.createElement(SystemStatusPanel, { language: "ru", modules: [{ ...module, status: { ...module.status, state: "OFFLINE", connection: "CONNECTED" } }], unavailable: false, loading: false }));
    const lost = renderToStaticMarkup(React.createElement(SystemStatusPanel, { language: "ru", modules: [{ ...module, status: { ...module.status, state: "OFFLINE", connection: "LOST" } }], unavailable: false, loading: false }));
    expect(connected).toContain('status-pill offline');
    expect(connected).toContain("Недоступен");
    expect(lost).toContain('status-pill offline');
    expect(lost).toContain('publisher-connection lost');
    expect(lost).toContain("Нет связи с издателем");
    const css = readFileSync(new URL("../src/App.css", import.meta.url), "utf8");
    expect(css).toContain(".offline{color:var(--aelev-warning)}");
    expect(css).toContain(".lost{color:var(--aelev-danger)}");
    expect(css).toContain(".publisher-connection{");
  });
  it("makes all component states unknown after an API failure", () => {
    const html = renderToStaticMarkup(React.createElement(SystemStatusPanel, { language: "ru", modules: [grouped], unavailable: true, loading: false }));
    expect(html.match(/status-pill unknown/g)).toHaveLength(4);
    expect(html).not.toContain('status-pill online');
    expect(html).not.toContain('status-pill offline');
  });
  it("keeps simple owners as rows without empty group controls", () => {
    const html = renderToStaticMarkup(React.createElement(SystemStatusPanel, { language: "ru", modules: [module], unavailable: false, loading: false }));
    expect(html).not.toContain('aria-expanded');
    expect(html).not.toContain('class="status-component"');
  });
  it("escapes injected component labels", () => {
    const unsafe = { ...grouped, components: [{ id: "process", title: { ru: "<script>attack</script>", en: "test" } }] };
    const html = renderToStaticMarkup(React.createElement(SystemStatusPanel, { language: "ru", modules: [unsafe], unavailable: false, loading: false }));
    expect(html).not.toContain("<script>");
    expect(html).toContain("&lt;script&gt;");
  });
  it("has no default participants", () => {
    const html = renderToStaticMarkup(React.createElement(SystemStatusPanel, { language: "ru", modules: [], unavailable: false, loading: false }));
    expect(html).toContain("Пространство готово"); expect(html).not.toContain("<table");
  });
  it("leaves the embedded page heading to its owner", () => {
    const source = readFileSync(new URL("../src/App.tsx", import.meta.url), "utf8");
    expect(source).not.toContain('className="page-intro"><h1>{selected?.title');
    expect(source).toContain('className="owner-frame"');
  });
  it.each(["ONLINE", "OFFLINE", "UNKNOWN"] as const)("renders the injected %s state", state => {
    const html = renderToStaticMarkup(React.createElement(SystemStatusPanel, { language: "en", modules: [{ ...module, status: { ...module.status, state } }], unavailable: false, loading: false }));
    expect(html).toContain("Arbitrary owner"); expect(html).toContain(state.toLowerCase());
  });
  it("does not treat refresh failure as offline", () => {
    const html = renderToStaticMarkup(React.createElement(SystemStatusPanel, { language: "ru", modules: [module], unavailable: true, loading: false }));
    expect(html).toContain("Нет данных"); expect(html).not.toContain("status-pill offline");
  });
  it("escapes owner labels", () => {
    const html = renderToStaticMarkup(React.createElement(SystemStatusPanel, { language: "ru", modules: [{ ...module, title: { ru: "<script>attack</script>", en: "test" } }], unavailable: false, loading: false }));
    expect(html).not.toContain("<script>");
  });
});
describe("presentation protocol", () => {
  it("rejects absent settings and plaintext", () => {
    expect(() => parseConfig({})).toThrow();
    expect(() => parseConfig({ keycloak_url: "http://identity.invalid", realm: "realm", client_id: "shell", poll_seconds: 1, stylesheet_sha256: "a".repeat(64) })).toThrow();
  });
  it("checks source and exact origin and transmits presentation only", () => {
    vi.stubGlobal("location", { origin: "https://shell.invalid" });
    const frame = { postMessage: vi.fn() } as unknown as Window;
    const event = { source: frame, origin: "https://owner.invalid", data: { type: "AELEV_UI_READY", version: 1 } } as unknown as MessageEvent;
    expect(presentationMessage({ ...event, origin: "https://attacker.invalid" } as MessageEvent, frame, "https://owner.invalid/page", "ru", "a".repeat(64))).toBe(false);
    expect(presentationMessage({ ...event, source: null } as MessageEvent, frame, "https://owner.invalid/page", "ru", "a".repeat(64))).toBe(false);
    expect(presentationMessage(event, frame, "https://owner.invalid/page", "ru", "a".repeat(64))).toBe(true);
    expect(frame.postMessage).toHaveBeenCalledWith({ type: "AELEV_UI_CONTEXT", version: 1, language: "ru", stylesheet_url: "https://shell.invalid/presentation/v6.css", stylesheet_sha256: "a".repeat(64) }, "https://owner.invalid");
    vi.unstubAllGlobals();
  });
  it("uses the exact loopback HTTP origin for a local embedded owner", () => {
    vi.stubGlobal("location", { origin: "https://shell.invalid" });
    const frame = { postMessage: vi.fn() } as unknown as Window;
    const event = { source: frame, origin: "http://127.0.0.1:18088", data: { type: "AELEV_UI_READY", version: 1 } } as unknown as MessageEvent;
    expect(presentationMessage(event, frame, "http://127.0.0.1:18088/", "ru", "a".repeat(64))).toBe(true);
    expect(frame.postMessage).toHaveBeenCalledWith(expect.objectContaining({ type: "AELEV_UI_CONTEXT" }), "http://127.0.0.1:18088");
    vi.unstubAllGlobals();
  });
});
