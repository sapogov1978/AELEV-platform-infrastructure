import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { beforeEach, afterEach, describe, expect, it, vi } from "vitest";
import { DataRefreshIndicator } from "../src/DataRefreshIndicator";
import { ownerRefreshMessage, refreshExpired, reportSummary, UNKNOWN_REFRESH } from "../src/dataRefresh";
import type { Module } from "../src/types";

const initial = { language: "ru" as const, sourceLabel: "Произвольный источник", pollSeconds: 60, refreshing: false, failed: false, updatedAt: null };
const received = Date.parse("2026-10-09T20:00:00Z");
const render = (props: Partial<React.ComponentProps<typeof DataRefreshIndicator>> = {}) => renderToStaticMarkup(React.createElement(DataRefreshIndicator, { ...initial, ...props }));

beforeEach(() => { vi.spyOn(Date, "now").mockReturnValue(received); });
afterEach(() => { vi.restoreAllMocks(); });

describe("shell refresh indicator", () => {
  it("does not claim an initial request has received fresh data", () => {
    const html = render({ refreshing: true });
    expect(html).toContain("Получение данных…");
    expect(html).toContain("data-refresh--loading");
    expect(html).not.toContain("<time");
    expect(html).not.toContain("Обновлено");
  });
  it("shows the minute interval and last successful response", () => {
    const html = render({ updatedAt: received });
    expect(html).toContain("data-refresh--ready");
    expect(html).toContain("Обновлено");
    expect(html).toContain('dateTime="2026-10-09T20:00:00.000Z"');
    expect(html).toContain("Данные обновляются раз в минуту");
    expect(html).toContain('role="status"');
  });
  it("retains the previous successful timestamp during the next request", () => {
    const html = render({ refreshing: true, updatedAt: received });
    expect(html).toContain("data-refresh--loading");
    expect(html).toContain("Последнее:");
    expect(html).toContain('dateTime="2026-10-09T20:00:00.000Z"');
  });
  it("retains the timestamp without calling failed data fresh", () => {
    const html = render({ failed: true, updatedAt: received });
    expect(html).toContain("data-refresh--error");
    expect(html).toContain("Ошибка обновления");
    expect(html).toContain('dateTime="2026-10-09T20:00:00.000Z"');
    expect(html).not.toContain("data-refresh--ready");
  });
  it("does not invent a timestamp after initial failure", () => {
    const html = render({ failed: true });
    expect(html).toContain("Ошибка обновления");
    expect(html).not.toContain("<time");
  });
  it("uses the actual interval rather than a hardcoded minute label", () => {
    expect(render({ pollSeconds: 15 })).toContain("Интервал обновления: 15 с");
    expect(render({ language: "en" })).toContain("Data refreshes every minute");
  });
  it("distinguishes reception from individual service freshness", () => {
    expect(render()).toContain("свежесть сообщений каждого сервиса указана отдельно");
    expect(render()).toContain("Данные ещё не получены");
  });
  it("does not reuse the shell interval before the owner reports", () => {
    const html = render({ pollSeconds: null });
    expect(html).toContain("Режим обновления не сообщён");
    expect(html).not.toContain("раз в минуту");
    expect(html).toContain("Источник: Произвольный источник");
  });
  it("does not call a successful list response fresh when reports are unknown", () => {
    const html = render({ updatedAt: received, reports: { total: 4, unknown: 1 } });
    expect(html).toContain("data-refresh--incomplete");
    expect(html).toContain("Есть неизвестные состояния");
    expect(html).toContain("Неизвестных состояний: 1 из 4");
    expect(html).toContain("Последнее получение списка");
    expect(html).not.toContain("data-refresh--ready");
  });
  it("does not claim received service data for an empty list", () => {
    const html = render({ updatedAt: received, reports: { total: 0, unknown: 0 } });
    expect(html).toContain("Нет активных публикаций");
    expect(html).toContain("data-refresh--empty");
    expect(html).not.toContain("data-refresh--ready");
  });
  it("does not confuse a fresh negative report with missing data", () => {
    const html = render({ updatedAt: received, reports: { total: 1, unknown: 0 } });
    expect(html).toContain("Состояния получены");
    expect(html).toContain("data-refresh--ready");
    expect(html).not.toContain("Доступен");
  });
  it.each(["ru", "en"] as const)("shows received statuses and a bare timestamp in %s", language => {
    const html = render({ language, updatedAt: received, reports: { total: 1, unknown: 0 } });
    expect(html).toContain(language === "ru" ? "Состояния получены" : "Status reports received");
    expect(html).toContain('class="data-refresh-time"><time');
    expect(html).toContain('dateTime="2026-10-09T20:00:00.000Z"');
    expect(html).not.toContain("Список:");
    expect(html).not.toContain("List:");
  });
  it("expires a success without requiring a new response", () => {
    vi.spyOn(Date, "now").mockReturnValue(received + 120_000);
    const html = render({ updatedAt: received });
    expect(html).toContain("data-refresh--stale");
    expect(html).toContain("Обновление задерживается");
    expect(html).not.toContain("data-refresh--ready");
  });
});

describe("reception freshness", () => {
  it("uses two actual source intervals and a precise expiry boundary", () => {
    expect(refreshExpired(received, 60, received + 119_999)).toBe(false);
    expect(refreshExpired(received, 60, received + 120_000)).toBe(true);
    expect(refreshExpired(received, 15, received + 30_000)).toBe(true);
    expect(refreshExpired(null, 60, received)).toBe(false);
    expect(refreshExpired(received, null, received + 120_000)).toBe(false);
  });
  it("summarizes injected components without product knowledge or inheriting the parent", () => {
    const module: Module = { schema_version: 1, module_id: "arbitrary", revision: 1, title: { ru: "Владелец", en: "Owner" }, sections: [], components: [{ id: "part", title: { ru: "Часть", en: "Part" } }], status: { sequence: 1, state: "OFFLINE", observed_at: new Date(received).toISOString() } };
    expect(reportSummary([module])).toEqual({ total: 2, unknown: 1, lost: 0 });
    expect(reportSummary([{ ...module, status: { ...module.status, components: [{ id: "part", state: "ONLINE", observed_at: new Date(received).toISOString() }] } }])).toEqual({ total: 2, unknown: 0, lost: 0 });
    expect(reportSummary([])).toEqual({ total: 0, unknown: 0, lost: 0 });
  });
  it("does not hide a lost publisher behind a successful list response", () => {
    const html = render({ updatedAt: received, reports: { total: 1, unknown: 1, lost: 1 } });
    expect(html).toContain("data-refresh--disconnected");
    expect(html).toContain("Потеряна связь с издателем");
    expect(html).not.toContain("data-refresh--ready");
  });
});

describe("owner refresh injection", () => {
  const frame = {} as Window;
  const report = { type: "AELEV_UI_REFRESH", version: 1, sequence: 1, phase: "READY", poll_seconds: 60, updated_at: "2026-10-09T20:00:00Z" };
  const event = (data: unknown = report) => ({ source: frame, origin: "https://owner.invalid", data }) as MessageEvent;
  const accept = (data: unknown = report) => ownerRefreshMessage(event(data), frame, "https://owner.invalid/page", UNKNOWN_REFRESH, received);
  it("accepts a report only from the exact active iframe and origin", () => {
    expect(accept()).toEqual({ sequence: 1, refreshing: false, failed: false, updatedAt: received, pollSeconds: 60 });
    expect(ownerRefreshMessage({ ...event(), origin: "https://attacker.invalid" } as MessageEvent, frame, "https://owner.invalid/page", UNKNOWN_REFRESH)).toBeNull();
    expect(ownerRefreshMessage({ ...event(), source: {} as Window } as MessageEvent, frame, "https://owner.invalid/page", UNKNOWN_REFRESH)).toBeNull();
    expect(ownerRefreshMessage(event(), null, "https://owner.invalid/page", UNKNOWN_REFRESH)).toBeNull();
  });
  it("keeps the last success on loading and failure", () => {
    const previous = accept()!;
    const loading = ownerRefreshMessage(event({ ...report, sequence: 2, phase: "LOADING", updated_at: null }), frame, "https://owner.invalid/page", previous, received)!;
    expect(loading.refreshing).toBe(true);
    expect(loading.updatedAt).toBe(received);
    const failed = ownerRefreshMessage(event({ ...report, sequence: 3, phase: "ERROR", updated_at: null }), frame, "https://owner.invalid/page", loading, received)!;
    expect(failed.failed).toBe(true);
    expect(failed.updatedAt).toBe(received);
  });
  it("ignores repeated and older sequences", () => {
    const previous = accept()!;
    expect(ownerRefreshMessage(event(), frame, "https://owner.invalid/page", previous)).toBeNull();
  });
  it.each([
    { version: 2 }, { sequence: 0 }, { sequence: true }, { sequence: 1.5 }, { sequence: Number.MAX_SAFE_INTEGER + 1 }, { poll_seconds: 0 }, { poll_seconds: "60" }, { phase: "UNSUPPORTED" }, { phase: ["LOADING"], updated_at: null }, { updated_at: null }, { updated_at: "not a timestamp" }, { updated_at: "2026-10-09T20:00:00" }, { updated_at: "2026-10-09T20:02:00Z" }, { updated_at: "2026-02-29T20:00:00Z" }, { updated_at: "2026-02-31T20:00:00Z" }, { phase: "ERROR" }, { token: "unwanted" },
  ])("rejects malformed, future or nontechnical metadata %j", patch => {
    expect(accept({ ...report, ...patch })).toBeNull();
  });
});
