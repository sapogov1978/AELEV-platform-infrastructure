import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { SystemStatusPanel } from "../src/SystemStatusPanel";
import type { Module } from "../src/types";

const owner: Module = {
  schema_version: 1, module_id: "be36a9c0-fbe9-4d14-8b77-3b5fc5c5ca55", revision: 1,
  title: { ru: "Произвольный владелец", en: "Arbitrary owner" },
  service_kind: "INFRASTRUCTURE", sections: [],
  status: { state: "ONLINE", observed_at: null, sequence: 1 },
};
const render = (module: Module, language: "ru" | "en" = "ru") => renderToStaticMarkup(
  React.createElement(SystemStatusPanel, { language, modules: [module], unavailable: false, loading: false }),
);

describe("owner-supplied external interfaces", () => {
  it.each(["http://localhost:18496/docs", "http://127.0.0.1:18496/docs", "http://[::1]:18496/docs"])("accepts only canonical loopback HTTP external links: %s", swagger_url => {
    expect(render({ ...owner, swagger_url })).toContain(`href="${swagger_url}"`);
  });
  it.each(["http://localhost.invalid/docs", "http://0.0.0.0/docs", "http://127.1/docs", "http://2130706433/docs", "http://0x7f000001/docs", "http://user@localhost/docs", "http://[::1]evil/docs", "http://localhost:0/docs", "http://localhost/#"])("rejects noncanonical or remote HTTP links: %s", swagger_url => {
    expect(render({ ...owner, swagger_url })).not.toContain('class="status-open"');
  });
  it.each(["ru", "en"] as const)("opens infrastructure UI with no session forwarding in %s", language => {
    const html = render({ ...owner, ui_url: "https://ui.owner.invalid/", swagger_url: "https://api.owner.invalid/docs" }, language);
    expect(html).toContain('href="https://ui.owner.invalid/"');
    expect(html).not.toContain('href="https://api.owner.invalid/docs"');
    expect(html).toContain('target="_blank" rel="noopener noreferrer" referrerPolicy="no-referrer"');
    expect(html).toContain(language === "ru" ? "Открыть" : "Open");
  });
  it("falls back to explicitly published Swagger, not inferred URLs", () => {
    expect(render({ ...owner, swagger_url: "https://api.owner.invalid/docs" })).toContain('href="https://api.owner.invalid/docs"');
    expect(render(owner)).not.toContain('class="status-open"');
    expect(render({ ...owner, ui_url: null, swagger_url: null })).not.toContain('class="status-open"');
  });
  it("shows only Swagger for a functional owner with injected sections", () => {
    const module = { ...owner, service_kind: "FUNCTIONAL" as const, ui_url: "https://ui.owner.invalid/", swagger_url: "https://api.owner.invalid/docs", sections: [{ id: "page", title: owner.title, url: "https://ui.owner.invalid/page" }] };
    const html = render(module);
    expect(html).toContain('href="https://api.owner.invalid/docs"');
    expect(html).not.toContain('href="https://ui.owner.invalid/"');
    expect(render({ ...module, swagger_url: null })).not.toContain('class="status-open"');
  });
  it("uses each child's own link without inheriting the parent link", () => {
    const module = { ...owner, ui_url: "https://ui.owner.invalid/", components: [
      { id: "api", title: owner.title, swagger_url: "https://api.owner.invalid/docs" },
      { id: "store", title: owner.title },
    ] };
    const html = render(module);
    expect(html.match(/class="status-open"/g)).toHaveLength(2);
    expect(html).toContain('href="https://api.owner.invalid/docs"');
  });
  it("does not show functional UI links on child rows either", () => {
    const html = render({ ...owner, service_kind: "FUNCTIONAL", sections: [{ id: "page", title: owner.title, url: "https://ui.owner.invalid/page" }], components: [{ id: "api", title: owner.title, ui_url: "https://ui.owner.invalid/", swagger_url: "https://api.owner.invalid/docs" }] });
    expect(html).not.toContain('href="https://ui.owner.invalid/"');
    expect(html).toContain('href="https://api.owner.invalid/docs"');
  });
  it.each(["javascript:alert(1)", "data:text/html,evil", "http://owner.invalid", "//owner.invalid", "/docs", "https://user:password@owner.invalid", "https://@owner.invalid", "https://owner.invalid/\x7f", "https://owner.invalid/#fragment", "https://owner.invalid\\evil", "https://owner.invalid/ bad", "https://owner.invalid/\n"])("rejects an unsafe URL defensively: %s", ui_url => {
    expect(render({ ...owner, ui_url })).not.toContain('class="status-open"');
  });
  it("keeps a published diagnostic link when the owner is offline", () => {
    const html = render({ ...owner, swagger_url: "https://api.owner.invalid/docs", status: { ...owner.status, state: "OFFLINE", connection: "LOST" } });
    expect(html).toContain("Нет связи");
    expect(html).toContain('href="https://api.owner.invalid/docs"');
  });
});
