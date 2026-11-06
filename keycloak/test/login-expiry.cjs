"use strict";

// Isolated integration probe: disposable loopback-only Keycloak, no live realm.
const assert = require("node:assert/strict");
const { execFileSync, spawnSync } = require("node:child_process");
const { createHash, randomBytes } = require("node:crypto");
const { mkdtempSync, writeFileSync, readFileSync, rmSync } = require("node:fs");
const { createServer } = require("node:http");
const { tmpdir } = require("node:os");
const { join } = require("node:path");
const { setTimeout: delay } = require("node:timers/promises");
const { chromium } = require(process.env.AELEV_INFRASTRUCTURE_KEYCLOAK_TEST_PLAYWRIGHT_MODULE || "playwright");
const image = process.env.AELEV_INFRASTRUCTURE_KEYCLOAK_TEST_IMAGE;
const realmName = "login-recovery-test";
const attemptLifetimeSeconds = 15;
const retainFailure = process.env.AELEV_INFRASTRUCTURE_KEYCLOAK_TEST_RETAIN_FAILURE === "1";
assert.ok(image, "set AELEV_INFRASTRUCTURE_KEYCLOAK_TEST_IMAGE to the built Keycloak image");
const startupSeconds = Number(process.env.AELEV_INFRASTRUCTURE_KEYCLOAK_TEST_STARTUP_TIMEOUT_SECONDS || "900");
assert.ok(Number.isInteger(startupSeconds) && startupSeconds >= 30 && startupSeconds <= 1800,
  "startup budget must be 30..1800 seconds");
const name = `aelev-keycloak-login-test-${process.pid}-${randomBytes(4).toString("hex")}`;
const network = `${name}-network`;
const directory = mkdtempSync(join(tmpdir(), "aelev-keycloak-login-"));
const docker = (...args) => execFileSync("docker", args, { encoding: "utf8", stdio: ["ignore", "pipe", "pipe"] }).trim();

async function main() {
  let browser;
  let container;
  let database;
  let networkCreated = false;
  let completed = false;
  const callback = createServer((request, response) => {
    response.writeHead(200, { "Content-Type": "text/html" });
    response.end("<h1>authenticated</h1>");
  });
  try {
    await new Promise((accept) => callback.listen(0, "127.0.0.1", accept));
    const redirect = `http://127.0.0.1:${callback.address().port}/callback`;
    const password = randomBytes(32).toString("hex");
    const realmFile = join(directory, `${realmName}-realm.json`);
    writeFileSync(realmFile, JSON.stringify({
      realm: realmName, enabled: true, sslRequired: "none", loginTheme: "aelev",
      accessCodeLifespanLogin: attemptLifetimeSeconds, accessCodeLifespanUserAction: attemptLifetimeSeconds,
      users: [{ username: "synthetic-user", enabled: true, firstName: "Synthetic", lastName: "User",
        email: "synthetic@example.invalid", emailVerified: true,
        credentials: [{ type: "password", value: password, temporary: false }] }],
      clients: [{ clientId: "login-test", publicClient: true, standardFlowEnabled: true,
        redirectUris: [redirect], attributes: { "pkce.code.challenge.method": "S256" } }],
    }), { mode: 0o644 }); // Private temporary directory; synthetic fixture only.
    const databaseSecret = join(directory, "database-password");
    writeFileSync(databaseSecret, randomBytes(32).toString("hex"), { mode: 0o644 });
    const subnet = process.env.AELEV_INFRASTRUCTURE_KEYCLOAK_TEST_SUBNET;
    docker("network", "create", "--label", "aelev.test=keycloak-login-recovery",
      ...(subnet ? ["--subnet", subnet] : []), network);
    networkCreated = true;
    database = docker("run", "--detach", "--rm", "--name", `${name}-database`,
      "--network", network, "--network-alias", "test-database",
      "--label", "aelev.test=keycloak-login-recovery", "--memory", "192m", "--cpus", "1",
      "--env", "POSTGRES_DB=keycloak_test", "--env", "POSTGRES_USER=keycloak_test",
      "--env", "POSTGRES_PASSWORD_FILE=/run/secrets/database-password", "--mount",
      `type=bind,source=${databaseSecret},target=/run/secrets/database-password,readonly`,
      process.env.AELEV_INFRASTRUCTURE_KEYCLOAK_TEST_DATABASE_IMAGE || "postgres:17.6-alpine");
    let databaseReady = false;
    for (let attempt = 0; attempt < 60; attempt++) {
      try {
        docker("exec", database, "pg_isready", "-U", "keycloak_test", "-d", "keycloak_test");
        databaseReady = true;
        break;
      } catch { await delay(1000); }
    }
    assert.ok(databaseReady, "isolated PostgreSQL did not start");
    container = docker("run", "--detach", "--name", name,
      "--network", network,
      "--label", "aelev.test=keycloak-login-recovery", "--memory", "1g", "--cpus", "2",
      "--publish", "127.0.0.1::8080", "--mount",
      `type=bind,source=${realmFile},target=/opt/keycloak/data/import/${realmName}-realm.json,readonly`,
      "--mount", `type=bind,source=${databaseSecret},target=/run/secrets/database-password,readonly`,
      "--mount", `type=bind,source=${databaseSecret},target=/run/secrets/admin-password,readonly`,
      "--env", "AELEV_KEYCLOAK_ADMIN_PASSWORD_FILE=/run/secrets/admin-password",
      "--env", "AELEV_KEYCLOAK_DATABASE_PASSWORD_FILE=/run/secrets/database-password",
      "--env", "KC_DB=postgres", "--env", "KC_DB_USERNAME=keycloak_test",
      "--env", "JAVA_OPTS_APPEND=-Xms64m -Xmx512m -XX:ActiveProcessorCount=2",
      "--env", "KC_DB_URL=jdbc:postgresql://test-database:5432/keycloak_test", image,
      "start", "--optimized", "--import-realm", "--http-enabled=true", "--hostname-strict=false", "--cache=local");
    const port = docker("port", container, "8080/tcp").split(":").at(-1);
    const base = `http://127.0.0.1:${port}/realms/${realmName}/protocol/openid-connect`;
    let ready = false;
    const startupDeadline = Date.now() + startupSeconds * 1000;
    while (Date.now() < startupDeadline) {
      const state = JSON.parse(docker("inspect", "--format", "{{json .State}}", container));
      if (!state.Running) {
        const output = spawnSync("docker", ["logs", "--tail", "80", container], { encoding: "utf8" });
        const logs = (output.stdout + output.stderr)
          .replaceAll(password, "[redacted]")
          .replaceAll(readFileSync(databaseSecret, "utf8"), "[redacted]");
        throw new Error(`isolated Keycloak exited early: exit=${state.ExitCode}, oom=${state.OOMKilled}\n${logs}`);
      }
      try {
        ready = (await fetch(`${base}/certs`, { signal: AbortSignal.timeout(1000) })).ok;
      } catch { /* Startup is bounded and does not touch a live service. */ }
      if (ready) break;
      await delay(1000);
    }
    assert.ok(ready, "isolated Keycloak did not start");
    browser = await chromium.launch({ headless: true });

    async function attempt({ baseline = false, expire = true, wrongPassword = false, missingSession = false,
      missingAllCookies = false, missingAfterWrongPassword = false } = {}) {
      const context = await browser.newContext();
      try {
        const page = await context.newPage();
        if (baseline) await page.route("**/js/login-refresh.js*", (route) => route.abort());
        const verifier = randomBytes(32).toString("base64url");
        const state = randomBytes(24).toString("base64url");
        const url = new URL(`${base}/auth`);
        url.search = new URLSearchParams({ client_id: "login-test", response_type: "code",
          redirect_uri: redirect, scope: "openid", state,
          code_challenge: createHash("sha256").update(verifier).digest("base64url"), code_challenge_method: "S256" });
        let posts = 0;
        page.on("request", (request) => {
          if (request.method() === "POST" && request.url().includes("/login-actions/authenticate")) posts++;
        });
        await page.goto(url.href);
        await page.waitForSelector("#username");
        if (expire) await delay((attemptLifetimeSeconds + 2) * 1000);
        if (missingSession) {
          // Model loss on an already-open page, after Keycloak's native
          // one-shot (1 s) cross-tab hash check, not during that safety check.
          await delay(2000);
          await context.clearCookies({ name: "AUTH_SESSION_ID" });
        }
        if (missingAllCookies) {
          await delay(2000);
          await context.clearCookies();
        }
        await page.locator("#username").fill("synthetic-user");
        await page.locator("#password").fill(wrongPassword ? randomBytes(32).toString("hex") : password);
        await Promise.all([
          page.waitForEvent("domcontentloaded"),
          page.locator("#kc-login").click(),
        ]);
        if (baseline || wrongPassword) {
          await page.waitForSelector("#username");
          assert.equal(posts, 1);
          assert.ok(!page.url().startsWith(redirect), "invalid first attempt unexpectedly succeeded");
          if (missingAfterWrongPassword) {
            await delay(2000);
            await context.clearCookies();
          }
          await page.locator("#username").fill("synthetic-user");
          await page.locator("#password").fill(password);
          await Promise.all([
            page.waitForEvent("domcontentloaded"),
            page.locator("#kc-login").click(),
          ]);
        }
        try {
          await page.waitForURL(`${redirect}?**`, { timeout: 20000 });
        } catch {
          const errors = await page.locator("#input-error, #kc-error-message, [role=alert]").allTextContents();
          throw new Error("authentication callback missing: " + JSON.stringify({
            baseline, expire, wrongPassword, missingSession, missingAllCookies, missingAfterWrongPassword, posts,
            path: new URL(page.url()).pathname,
            errors: errors.map((value) => value.trim().slice(0, 300)),
          }));
        }
        const result = new URL(page.url());
        assert.equal(result.searchParams.get("state"), state);
        assert.equal(posts, baseline || wrongPassword ? 2 : 1);
        const token = await fetch(`${base}/token`, {
          method: "POST", headers: { "Content-Type": "application/x-www-form-urlencoded" },
          body: new URLSearchParams({ grant_type: "authorization_code", client_id: "login-test",
            redirect_uri: redirect, code: result.searchParams.get("code"), code_verifier: verifier }),
        });
        assert.equal(token.status, 200, "PKCE code exchange failed");
        assert.ok((await token.json()).access_token);
      } finally {
        await context.close();
      }
    }
    await attempt({ baseline: true });
    console.log("PASS: old behavior reproduced: expired form needs two credential POSTs");
    await attempt();
    console.log("PASS: expired form succeeds with one credential POST; state and S256 PKCE preserved");
    await attempt({ expire: false });
    console.log("PASS: fresh form succeeds with one credential POST");
    await attempt({ expire: false, wrongPassword: true });
    console.log("PASS: wrong password remains rejected; no automatic credential retry");
    await attempt({ baseline: true, expire: false, missingSession: true });
    console.log("PASS: missing authentication session reproduces the original two-submit failure");
    await attempt({ expire: false, missingSession: true });
    console.log("PASS: signed restart cookie restores a missing session before a single credential POST");
    await attempt({ expire: false, missingAllCookies: true });
    console.log("PASS: all authentication cookies lost; fresh OIDC request succeeds with one credential POST and S256 PKCE");
    await attempt({ expire: false, wrongPassword: true, missingAfterWrongPassword: true });
    console.log("PASS: lost cookies after a wrong password; exact per-tab OIDC request restores sign-in without retrying credentials");
    completed = true;
  } finally {
    await browser?.close();
    callback.close();
    if (retainFailure && !completed) {
      console.error(JSON.stringify({ event: "synthetic_test_resources_retained", container: name,
        database: `${name}-database`, network, directory }));
      process.exitCode = 1;
    } else {
      if (container) {
        assert.equal(docker("inspect", "--format", '{{index .Config.Labels "aelev.test"}}', container), "keycloak-login-recovery");
        docker("stop", "--time", "15", container);
        docker("rm", container); // Remove only this labelled disposable container.
      }
      if (database) {
        assert.equal(docker("inspect", "--format", '{{index .Config.Labels "aelev.test"}}', database), "keycloak-login-recovery");
        docker("stop", "--time", "15", database);
      }
      if (networkCreated) docker("network", "rm", network);
      rmSync(directory, { recursive: true }); // Exact private directory created above.
    }
  }
}
main().catch((error) => { console.error(error.name + ": " + error.message); process.exitCode = 1; });
