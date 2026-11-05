"""Verify the real Grafana operator identity and Keycloak SSO reuse."""

from __future__ import annotations

import argparse
from importlib import import_module
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

OPERATOR = "platform-operator"
GRAFANA_COOKIES = {
    "grafana_session",
    "grafana_session_expiry",
    "grafana_remember",
    "grafana_sso",
    "oauth_code_verifier",
    "oauth_state",
    "redirectTo",
}


def operator_identity(page: Any) -> dict[str, object]:
    """Read only the fields needed to prove the effective Grafana role."""
    result: dict[str, object] = page.evaluate("""async () => {
            const [userResponse, orgsResponse] = await Promise.all([
                fetch('/api/user'),
                fetch('/api/user/orgs'),
            ]);
            if (!userResponse.ok || !orgsResponse.ok) {
                return {status: userResponse.status, orgsStatus: orgsResponse.status};
            }
            const user = await userResponse.json();
            const orgs = await orgsResponse.json();
            return {
                status: userResponse.status,
                login: user.login,
                roles: orgs.map((org) => org.role),
                isGrafanaAdmin: user.isGrafanaAdmin,
            };
        }""")
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--grafana-url", required=True)
    parser.add_argument("--operator-password-file", required=True, type=Path)
    parser.add_argument("--chromium", required=True, type=Path)
    parser.add_argument("--allow-local-certificate", action="store_true")
    args = parser.parse_args()
    grafana_url = args.grafana_url.rstrip("/")
    parsed = urlsplit(grafana_url)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.path
        or parsed.query
        or parsed.fragment
    ):
        parser.error("grafana URL must be a credential-free HTTPS origin")
    if args.allow_local_certificate and parsed.hostname not in {
        "localhost",
        "127.0.0.1",
        "::1",
    }:
        parser.error("certificate exception requires explicit loopback")
    if not args.chromium.is_file() or not args.operator_password_file.is_file():
        parser.error("Chromium and operator password file must exist")

    support = import_module("playwright.sync_api")
    expected = {
        "status": 200,
        "login": OPERATOR,
        "roles": ["Admin"],
        "isGrafanaAdmin": False,
    }
    with support.sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            executable_path=str(args.chromium), headless=True
        )
        try:
            context = browser.new_context(
                ignore_https_errors=args.allow_local_certificate
            )
            page = context.new_page()
            page.goto(grafana_url, wait_until="domcontentloaded")
            page.locator("#username").wait_for(timeout=15_000)
            page.locator("#username").fill(OPERATOR)
            page.locator("#password").fill(
                args.operator_password_file.read_text().strip()
            )
            page.locator("#kc-login").click()
            page.wait_for_url(grafana_url + "/**", timeout=30_000)
            if operator_identity(page) != expected:
                raise RuntimeError("unexpected Grafana operator identity or role")

            cookies = context.cookies()
            context.clear_cookies()
            context.add_cookies(
                [cookie for cookie in cookies if cookie["name"] not in GRAFANA_COOKIES]
            )
            page.close()
            page = context.new_page()
            try:
                page.goto(grafana_url, wait_until="domcontentloaded")
            except support.Error:
                pass
            page.wait_for_url(grafana_url + "/**", timeout=30_000)
            if page.locator("#username").count() != 0:
                raise RuntimeError("Keycloak requested credentials despite active SSO")
            if operator_identity(page) != expected:
                raise RuntimeError(
                    "SSO returned an unexpected Grafana identity or role"
                )
        finally:
            browser.close()
    print(
        "PASS: platform-operator authenticated as organization Admin and reused Keycloak SSO"
    )


if __name__ == "__main__":
    main()
