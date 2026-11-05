"use strict";

const assert = require("node:assert/strict");
const { randomBytes } = require("node:crypto");
const { readFileSync } = require("node:fs");
const { resolve } = require("node:path");
const { test, before, after } = require("node:test");
const { chromium } = require(process.env.AELEV_INFRASTRUCTURE_KEYCLOAK_TEST_PLAYWRIGHT_MODULE || "playwright");
const script = readFileSync(resolve(__dirname, "../themes/aelev/login/resources/js/login-refresh.js"), "utf8");
const base = "https://idp.invalid/realms/test/login-actions/authenticate";
const oldAction = `${base}?client_id=test&tab_id=old&client_data=original&session_code=old`;
const newAction = `${base}?client_id=test&tab_id=new&client_data=original&session_code=new`;
const authorization = "https://idp.invalid/realms/test/protocol/openid-connect/auth?" +
  new URLSearchParams({ client_id: "test", response_type: "code", redirect_uri: "https://app.invalid/callback",
    state: "original-state", nonce: "original-nonce", code_challenge: "original-challenge", code_challenge_method: "S256" });
let browser;
before(async () => { browser = await chromium.launch({ headless: true }); });
after(async () => { await browser?.close(); });

function html(action, extra = "", withScript = false) {
  return `<html lang="ru"><body><form id="kc-form-login" method="post" onsubmit="login.disabled = true; return true;" action="${action}">
    <input name="username" required><input name="password" type="password" required>
    <input name="credentialId" type="hidden" value="">${extra}
    <button id="kc-login" name="login" type="submit">Sign in</button>
    </form>${withScript ? `<script>${script}</script>` : ""}</body></html>`;
}

async function fixture(t, reply = () => ({ body: html(newAction) }), initial = oldAction, storageDisabled = false) {
  const context = await browser.newContext();
  t.after(() => context.close());
  if (storageDisabled) await context.addInitScript(() => {
    Object.defineProperty(window, "sessionStorage", { get() { throw new DOMException("Blocked", "SecurityError"); } });
  });
  const page = await context.newPage();
  const requests = [];
  await page.route("https://idp.invalid/**", async (route) => {
    const request = route.request();
    if (request.isNavigationRequest() && request.method() === "GET" && !requests.length) {
      requests.push("initial");
      await route.fulfill({ contentType: "text/html", body: html(oldAction, "", true) });
    } else if (request.method() === "GET") {
      assert.equal(request.postData(), null);
      assert.equal(new URL(request.url()).searchParams.has("session_code"), false);
      const kind = new URL(request.url()).pathname.endsWith("/auth") ? "authorize" : "check";
      if (kind === "authorize") assert.equal(request.url(), authorization);
      requests.push(kind);
      await route.fulfill({ contentType: "text/html", ...reply(kind) });
    } else {
      assert.equal(request.method(), "POST");
      assert.equal(new URL(request.url()).searchParams.get("session_code"), "new");
      requests.push("post");
      await route.fulfill({ contentType: "text/html", body: "<h1>complete</h1>" });
    }
  });
  await page.goto(initial);
  await page.locator('[name="username"]').fill("synthetic-user");
  await page.locator('[name="password"]').fill(randomBytes(24).toString("hex"));
  return { page, requests };
}

test("one credential POST follows a fresh server GET", async (t) => {
  const { page, requests } = await fixture(t);
  await page.locator("#kc-login").click();
  await page.waitForSelector("h1");
  assert.deepEqual(requests, ["initial", "check", "post"]);
});

test("lost restart cookie starts the original OIDC request before one credential POST", async (t) => {
  const { page, requests } = await fixture(t, (kind) => kind === "check"
    ? { status: 400, body: "<h1>Cookie not found</h1>" } : { body: html(newAction) }, authorization);
  await page.locator("#kc-login").click();
  await page.waitForSelector("h1");
  assert.deepEqual(requests, ["initial", "check", "authorize", "post"]);
});

test("original OIDC page recovers even when sessionStorage is disabled", async (t) => {
  const { page, requests } = await fixture(t, (kind) => kind === "check"
    ? { status: 400, body: "<h1>Cookie not found</h1>" } : { body: html(newAction) }, authorization, true);
  await page.locator("#kc-login").click();
  await page.waitForSelector("h1");
  assert.deepEqual(requests, ["initial", "check", "authorize", "post"]);
});

test("the exact per-tab request survives a wrong-password page", async (t) => {
  const { page, requests } = await fixture(t, (kind) => kind === "check"
    ? { status: 400, body: "<h1>Cookie not found</h1>" } : { body: html(newAction) }, authorization);
  // A server-rendered form after a rejected password has a login-actions URL.
  await page.route("**/wrong-password", (route) => route.fulfill({ contentType: "text/html", body: html(oldAction, "", true) }));
  await page.goto("https://idp.invalid/realms/test/wrong-password");
  await page.locator('[name="username"]').fill("synthetic-user");
  await page.locator('[name="password"]').fill(randomBytes(24).toString("hex"));
  await page.locator("#kc-login").click();
  await page.waitForSelector("h1");
  assert.deepEqual(requests, ["initial", "check", "authorize", "post"]);
});

for (const [name, recovery] of [
  ["second 400", { status: 400, body: "<h1>Cookie still missing</h1>" }],
  ["unavailable authorization", { status: 503, body: "unavailable" }],
  ["new OTP step", { body: '<form id="kc-otp-login-form"></form>' }],
  ["changed OIDC context", { body: html(newAction.replace("client_data=original", "client_data=foreign")) }],
]) {
  test(`${name} cannot loop or send credentials during recovery`, async (t) => {
    const { page, requests } = await fixture(t, (kind) => kind === "check"
      ? { status: 400, body: "<h1>Cookie not found</h1>" } : recovery, authorization);
    await page.locator("#kc-login").click();
    await page.waitForSelector('[role="alert"]:visible');
    assert.deepEqual(requests, ["initial", "check", "authorize"]);
    assert.equal(await page.locator("#kc-login").isEnabled(), true);
  });
}

test("5xx never triggers a new authorization even when its URL is available", async (t) => {
  const { page, requests } = await fixture(t, () => ({ status: 503, body: "unavailable" }), authorization);
  await page.locator("#kc-login").click();
  await page.waitForSelector('[role="alert"]:visible');
  assert.deepEqual(requests, ["initial", "check"]);
});

for (const saved of [
  { href: authorization, clientData: "foreign" },
  { href: authorization.replace("idp.invalid", "foreign.invalid"), clientData: "original" },
  { href: authorization.replace("/test/", "/foreign/"), clientData: "original" },
  { href: authorization.replace("client_id=test", "client_id=foreign"), clientData: "original" },
]) {
  test("foreign saved request cannot restart the login", async (t) => {
    const { page, requests } = await fixture(t, () => ({ status: 400, body: "<h1>Cookie not found</h1>" }));
    await page.evaluate((value) => sessionStorage.setItem("aelev.login-request:/realms/test", JSON.stringify(value)), saved);
    await page.route("**/another-form", (route) => route.fulfill({ contentType: "text/html", body: html(oldAction, "", true) }));
    await page.goto("https://idp.invalid/realms/test/another-form");
    await page.locator('[name="username"]').fill("synthetic-user");
    await page.locator('[name="password"]').fill(randomBytes(24).toString("hex"));
    await page.locator("#kc-login").click();
    await page.waitForSelector('[role="alert"]:visible');
    assert.deepEqual(requests, ["initial", "check"]);
  });
}

for (const [name, reply] of [
  ["network failure", () => ({ status: 503, body: "unavailable" })],
  ["non-HTML reply", () => ({ contentType: "application/json", body: "{}" })],
  ["OTP step", () => ({ body: '<form id="kc-otp-login-form"></form>' })],
  ["another realm", () => ({ body: html(newAction.replace("/test/", "/other/")) })],
  ["another client", () => ({ body: html(newAction.replace("client_id=test", "client_id=other")) })],
  ["another OIDC context", () => ({ body: html(newAction.replace("client_data=original", "client_data=other")) })],
  ["foreign origin", () => ({ body: html(newAction.replace("idp.invalid", "foreign.invalid")) })],
  ["missing code", () => ({ body: html(newAction.replace("&session_code=new", "")) })],
  ["changed hidden challenge", () => ({ body: html(newAction, '<input type="hidden" name="challenge" value="other">') })],
  ["changed credential selection", () => ({ body: html(newAction).replace('name="credentialId" type="hidden" value=""', 'name="credentialId" type="hidden" value="other"') })],
]) {
  test(`${name} does not send credentials`, async (t) => {
    const { page, requests } = await fixture(t, reply);
    await page.locator("#kc-login").click();
    await page.waitForSelector('[role="alert"]:visible');
    assert.deepEqual(requests, ["initial", "check"]);
    assert.equal(await page.locator("#kc-login").isEnabled(), true);
  });
}

test("retry after a failed check sends credentials only once", async (t) => {
  let attempts = 0;
  const { page, requests } = await fixture(t, () => ++attempts === 1
    ? { status: 503, body: "unavailable" } : { body: html(newAction) });
  await page.locator("#kc-login").click();
  await page.waitForSelector('[role="alert"]:visible');
  await page.locator("#kc-login").click();
  await page.waitForSelector("h1");
  assert.deepEqual(requests, ["initial", "check", "check", "post"]);
});

test("duplicate submit events do not duplicate a credential POST", async (t) => {
  const { page, requests } = await fixture(t);
  await page.locator("form").evaluate((form) => {
    form.requestSubmit();
    form.requestSubmit();
  });
  await page.waitForSelector("h1");
  assert.deepEqual(requests, ["initial", "check", "post"]);
});

test("native validation still prevents submission", async (t) => {
  const { page, requests } = await fixture(t);
  await page.locator('[name="password"]').fill("");
  await page.locator("#kc-login").click();
  assert.deepEqual(requests, ["initial"]);
});

for (const attribute of ["method", "formaction"]) {
  test(`changed ${attribute} cannot bypass the checked action`, async (t) => {
    const { page, requests } = await fixture(t);
    if (attribute === "method") await page.locator("form").evaluate((form) => { form.method = "get"; });
    else await page.locator("#kc-login").evaluate((button) => button.setAttribute("formaction", "https://foreign.invalid/"));
    await page.locator("#kc-login").click();
    await page.waitForSelector('[role="alert"]:visible');
    assert.deepEqual(requests, ["initial"]);
  });
}

test("leaving aborts the check; returning permits a manual retry", async (t) => {
  const { page, requests } = await fixture(t);
  let held;
  const pattern = "**/login-actions/authenticate?**";
  const hold = (route) => {
    if (route.request().method() === "GET" && !route.request().isNavigationRequest()) held = route;
    else return route.fallback();
  };
  await page.route(pattern, hold);
  await Promise.all([
    page.waitForRequest((request) => request.method() === "GET" && !request.isNavigationRequest()),
    page.locator("#kc-login").click(),
  ]);
  await page.evaluate(() => window.dispatchEvent(new Event("pagehide")));
  await page.waitForFunction(() => !document.getElementById("kc-login").disabled);
  assert.deepEqual(requests, ["initial"]);
  await held.abort().catch(() => {});
  await page.unroute(pattern, hold);
  await page.evaluate(() => window.dispatchEvent(new Event("pageshow")));
  await page.locator("#kc-login").click();
  await page.waitForSelector("h1");
  assert.deepEqual(requests, ["initial", "check", "post"]);
});

for (const passwordOnly of [true, false]) {
  test(`${passwordOnly ? "password-only" : "OTP"} step retains native submission`, async (t) => {
    const context = await browser.newContext();
    t.after(() => context.close());
    const page = await context.newPage();
    let checks = 0;
    await page.route("https://idp.invalid/**", async (route) => {
      if (route.request().method() === "POST") {
        await route.fulfill({ contentType: "text/html", body: "<h1>native</h1>" });
      } else {
        checks++;
        await route.fulfill({ contentType: "text/html", body: `<html><form id="${passwordOnly ? "kc-form-login" : "kc-otp-login-form"}" method="post" action="${oldAction}">
          <input name="${passwordOnly ? "password" : "otp"}" type="${passwordOnly ? "password" : "text"}">
          <button>Submit</button></form><script>${script}</script></html>` });
      }
    });
    await page.goto(oldAction);
    await page.locator("input").fill(randomBytes(24).toString("hex"));
    await page.locator("button").click();
    await page.waitForSelector("h1");
    assert.equal(checks, 1);
  });
}
