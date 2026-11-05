import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ownerRefreshMessage, UNKNOWN_REFRESH, type RefreshState } from "../src/dataRefresh";
import { presentationMessage } from "../src/types";

function example(name: string): string {
  const text = readFileSync(new URL("../USER_MANUAL.md", import.meta.url), "utf8");
  const matches = [...text.matchAll(new RegExp(`<!-- example: ${name} -->\\n\x60\x60\x60\\w+\\n([\\s\\S]*?)\\n\x60\x60\x60`, "g"))];
  expect(matches).toHaveLength(1);
  return matches[0][1];
}

const digest = createHash("sha256").update(readFileSync(new URL("../public/presentation/v6.css", import.meta.url))).digest("hex");
const context = { type: "AELEV_UI_CONTEXT", version: 1, language: "ru", stylesheet_url: "https://shell.invalid/presentation/v6.css", stylesheet_sha256: digest };
const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor;
type Timer = { callback: () => Promise<void> | void; delay: number };

function page(options: { standalone?: boolean; cssFails?: boolean; config?: unknown } = {}) {
  vi.stubGlobal("location", { origin: "https://shell.invalid" });
  const listeners = new Map<string, (event: MessageEvent) => void>();
  const timers = new Map<number, Timer>();
  const states: RefreshState[] = [];
  let counter = 0;
  let previous = UNKNOWN_REFRESH;
  let failing = false;
  const heading = { textContent: "" };
  const result = { textContent: "" };
  const stylesheet = { rel: "", href: "", integrity: "", crossOrigin: "", onload: () => {}, onerror: () => {} };
  const emit = (data: unknown = context, origin = "https://shell.invalid", source: unknown = parent) => {
    listeners.get("message")?.({ data, origin, source } as MessageEvent);
  };
  const frame = { postMessage: (data: unknown, origin: string) => {
    expect(origin).toBe("https://owner.invalid");
    emit(data); // targetOrigin is the destination; MessageEvent.origin is the sender.
  } } as Window;
  const parent = { postMessage: vi.fn((data: Record<string, unknown>, origin: string) => {
    expect(origin).toBe("https://shell.invalid");
    if (data.type === "AELEV_UI_READY") {
      expect(presentationMessage({ data, source: frame, origin: "https://owner.invalid" } as MessageEvent, frame, "https://owner.invalid/", "ru", digest)).toBe(true);
    } else {
      const accepted = ownerRefreshMessage({ data, source: frame, origin: "https://owner.invalid" } as MessageEvent, frame, "https://owner.invalid/", previous);
      expect(accepted).not.toBeNull();
      previous = accepted!;
      states.push(previous);
    }
  }) };
  const window = {
    parent: parent as unknown,
    addEventListener: (name: string, listener: (event: MessageEvent) => void) => listeners.set(name, listener),
    removeEventListener: (name: string) => listeners.delete(name),
  };
  if (options.standalone) window.parent = window;
  const document = {
    documentElement: { lang: "ru" },
    getElementById: (id: string) => id === "heading" ? heading : result,
    createElement: () => stylesheet,
    head: { append: () => options.cssFails ? stylesheet.onerror() : stylesheet.onload() },
  };
  const fetch = vi.fn(async (url: string, init: { signal?: AbortSignal }) => {
    if (url === "/ui-config.json") return { ok: true, json: async () => options.config ?? { shell_origin: "https://shell.invalid", poll_seconds: 60 } };
    expect(url).toBe("/api/readyz");
    expect(init.signal).toBeInstanceOf(AbortSignal);
    if (failing) throw new Error("private response not for parent");
    return { ok: true, json: async () => ({ status: "ready" }) };
  });
  const setTimeout = (callback: Timer["callback"], delay: number) => {
    const id = ++counter;
    timers.set(id, { callback, delay });
    return id;
  };
  const clearTimeout = (id: number) => timers.delete(id);
  const run = new AsyncFunction("window", "document", "fetch", "setTimeout", "clearTimeout", "btoa", example("owner-javascript"))(
    window, document, fetch, setTimeout, clearTimeout, (bytes: string) => Buffer.from(bytes, "binary").toString("base64"),
  );
  const tick = async () => {
    expect(timers.size).toBe(1);
    const [id, next] = [...timers][0];
    expect(next.delay).toBe(60_000);
    timers.delete(id);
    await next.callback();
  };
  return { run, emit, parent, document, heading, result, stylesheet, states, timers, fetch, tick,
    fail: (value: boolean) => { failing = value; },
    close: () => listeners.get("pagehide")?.({} as MessageEvent),
    listenerCount: () => listeners.has("message"),
  };
}

afterEach(() => vi.unstubAllGlobals());

describe("manual browser example", () => {
  it("completes the real presentation and refresh protocols with pinned SRI", async () => {
    const p = page();
    await p.run;
    expect(p.stylesheet.href).toBe(context.stylesheet_url);
    expect(p.stylesheet.integrity).toBe("sha256-" + Buffer.from(digest, "hex").toString("base64"));
    expect(p.stylesheet.crossOrigin).toBe("anonymous");
    expect(p.states.map(state => state.refreshing)).toEqual([true, true, false]);
    expect(p.states.at(-1)?.updatedAt).toBeTypeOf("number");
    expect(p.result.textContent).toBe("Backend готов");
    expect(example("owner-html")).toContain('src="/owner-bridge.js"');
    expect(example("owner-html")).not.toContain("<script>");
    expect(p.fetch.mock.calls.map(call => call[0])).toEqual(["/ui-config.json", "/api/readyz"]);
  });
  it("changes language without resetting success or sequence", async () => {
    const p = page();
    await p.run;
    const before = p.states.at(-1)!;
    p.emit({ ...context, language: "en" });
    expect(p.document.documentElement.lang).toBe("en");
    expect(p.heading.textContent).toBe("Example service");
    expect(p.result.textContent).toBe("Backend ready");
    expect(p.states.at(-1)?.updatedAt).toBe(before.updatedAt);
    expect(p.states.at(-1)?.sequence).toBe(before.sequence + 1);
    expect(p.fetch).toHaveBeenCalledTimes(2);
  });
  it("polls sequentially and retains last success during failure and recovery", async () => {
    const p = page();
    await p.run;
    const before = p.states.at(-1)!.updatedAt;
    p.fail(true);
    await p.tick();
    expect(p.states.at(-1)).toMatchObject({ failed: true, updatedAt: before, pollSeconds: 60 });
    expect(p.parent.postMessage.mock.calls.at(-1)?.[0]).toMatchObject({ phase: "ERROR", updated_at: null });
    expect(p.result.textContent).not.toContain("private response");
    p.fail(false);
    await p.tick();
    expect(p.states.at(-1)).toMatchObject({ failed: false, refreshing: false });
    expect(p.states.map(state => state.sequence)).toEqual([1, 2, 3, 4, 5, 6, 7]);
    expect(p.timers.size).toBe(1);
  });
  it("ignores wrong origins, sources, versions, extra fields and unpinned styles", async () => {
    const p = page();
    await p.run;
    const count = p.parent.postMessage.mock.calls.length;
    p.emit(context, "https://attacker.invalid");
    p.emit(context, "https://shell.invalid", {});
    for (const patch of [{ version: 2 }, { language: "xx" }, { token: "not-a-context-field" }, { stylesheet_sha256: "a".repeat(64) }, { stylesheet_url: "https://attacker.invalid/v2.css" }]) p.emit({ ...context, ...patch });
    expect(p.parent.postMessage).toHaveBeenCalledTimes(count);
    expect(p.document.documentElement.lang).toBe("ru");
  });
  it("does not poll or claim success after stylesheet verification fails", async () => {
    const p = page({ cssFails: true });
    await p.run;
    expect(p.fetch).toHaveBeenCalledTimes(1);
    expect(p.states.at(-1)).toMatchObject({ failed: true, updatedAt: null });
    expect(p.timers.size).toBe(0);
  });
  it("releases its timer, active signal and listener when the page closes", async () => {
    const p = page();
    await p.run;
    p.close();
    expect(p.timers.size).toBe(0);
    expect(p.fetch.mock.calls.at(-1)?.[1].signal?.aborted).toBe(true);
    expect(p.listenerCount()).toBe(false);
    const count = p.parent.postMessage.mock.calls.length;
    p.emit();
    expect(p.parent.postMessage).toHaveBeenCalledTimes(count);
  });
  it("works independently without shell context or borrowed tokens", async () => {
    const p = page({ standalone: true });
    await p.run;
    expect(p.result.textContent).toBe("Backend готов");
    expect(p.parent.postMessage).not.toHaveBeenCalled();
    expect(p.fetch.mock.calls.map(call => call[0])).toEqual(["/ui-config.json", "/api/readyz"]);
  });
  it.each([
    { shell_origin: "http://shell.invalid", poll_seconds: 60 },
    { shell_origin: "https://shell.invalid/path", poll_seconds: 60 },
    { shell_origin: "https://shell.invalid", poll_seconds: 0 },
  ])("rejects invalid derived config %j", async config => {
    const p = page({ config });
    await expect(p.run).rejects.toThrow("Invalid UI config");
    expect(p.parent.postMessage).not.toHaveBeenCalled();
  });
});
