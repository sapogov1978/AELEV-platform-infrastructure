# Platform UI network profile — component result, 2026-11-19

**PASS for this component scope.** The six platform acceptance outcomes remain open. This is a local Linux/arm64 qualification of independently placed roles, not release admission or proof of physically separate computers. The approved [pre-edit plan](network-profile-plan-20261119.md) estimated 4–6 hours; this block ran approximately 12:55–14:02 UTC, including native build, OIDC, fault checks and private fixture cleanup. Report/delivery work follows separately.

## Delivered behavior and compatibility

The additional owner `compose.network.yml` publishes an mTLS API with explicit bind/port and remote probe, without relying on a sibling Compose service alias or shared bridge. Nginx selects an explicit API route and DNS TLS name. API admits metadata only from the configured proxy SPIFFE URI extracted from the verified TLS connection; headers cannot supply that identity. Publisher→Nginx certificate verification and JWT issuer/audience/roles remain separate checks. All accepted dependency/base-image pins are unchanged.

The stdlib-only renderer accepts explicit network parameters and `--role nginx|api|provision|all`; each role checks only its own secret files and emits only its own configuration. The four no-argument legacy outputs match actual immutable source `696a190` SHA-256 values. The new network runtime directory has mode `0750` and configured secret GID even under `umask 077`. The original env schema remains strict; new Compose-only settings have a separate public example.

Nginx liveness remains local. Network UI readiness checks both the admitted proxy connection and API storage through `/api/proxy-readyz`; transport/auth/backend failures become 503. Own API probes retain `/api/readyz`. Metadata response codes, publisher ownership/revisions/sequence/tombstones, browser protocol, presentation bytes and serialized single-writer SQLite remain unchanged. Swagger is intentionally disabled in this owner; actual `/docs` and `/openapi.json` return 404 at the API.

## Green evidence

- Host: **295 passed**; Linux/Python 3.13.7 checks: **294 passed, 1 skipped**. The skip is specifically the additional macOS system LibreSSL signer. Two existing Starlette/httpx/AnyIO deprecation warnings remain. Ruff and strict typing of nine affected Python files pass; ten deployment-catalogue tests pass. Catalogue stays **95 roles: 57 runtime, 1 node, 37 operations**.
- **29 actual runtime checks** pass. Canonical UI, API, publisher, Keycloak and its PostgreSQL have pairwise disjoint networks. Each uses owner Compose with `--no-deps`; all cross-placement traffic uses published endpoints. Actual owner provision configures PKCE S256, operator and roles; publisher provision obtains its own real confidential client. Real Keycloak-signed viewer/client-credentials tokens exercise API JWKS verification, not a unit authenticator.
- The actual publisher reports nginx/API/publisher ONLINE. Anonymous, invalid JWT, real wrong audience, missing viewer role, publisher JWT without publisher mTLS, and viewer JWT attempting publication with a valid publisher certificate are refused. Published API refuses probe/foreign proxy identities even with valid JWT and forged publisher header; missing client certificate, wrong server CA and wrong hostname fail TLS.
- Reloading the actual Nginx with a same-CA foreign proxy identity yields UI readiness 503, metadata 403 and liveness 200. Restoring its permitted identity restores readiness. Stopping API also yields readiness 503 with liveness 200.
- While API is unavailable, the publisher journals pending sequence **2**. SIGKILL/restart preserves its UUID, sequence, withdrawal flag and exact pending bytes. After API restart its audit contains the canonical accepted digest `209baf075f2d77cf5ce44d92b9e1f9b096b79334e0559a776ff51dbbad8fe4fb`; publication becomes ONLINE and pending is acknowledged. Later observation reached sequence 22. This proves component restart/replay, not the full platform backup/restore/soak outcome.
- A separate Linux tmpfs DAC check avoids Desktop bind UID translation: UID 101 with GID 20 reads rendered API config; GID 21 receives PermissionError. The root is `0750`, GID 20, generated under `umask 077`.

Exact images, runtime versions, 18 build/source/config/test bindings and actual check results are in [bindings](network-profile-bindings-20261119.json). API and publisher use Python 3.13.7/OpenSSL 3.5.1. API SQLite remains 3.49.2 under its accepted serialized single-writer mitigation; publisher SQLite is 3.53.4. The new transport work does not close final security/audit gates or change those versions. The checks stage installs its own test tools; its libraries are not evidence of runtime library versions.

## Preserved failed observations

Phase 1 retained the initial fixture import/certificate setup failures and the prior operator GID 0 refusal, followed by its green packaged run. During this phase:

1. Synthetic publisher credentials were initially named `client.*`, while the pinned SDK expects `publisher.*`. Actual publisher exited before publication. Only disposable inputs were corrected.
2. The replay assertion initially read a still-ONLINE previous status before pending delivery. Its bounded observation was corrected to require the pending sequence, then the existing receiver audit established acceptance. The fault scenario was not rerun to erase that observation.
3. Exporting a temporary SQLite backup with Docker archive/copy did not find the tmpfs file. Export through the owning container's Python byte stream succeeded. The first cleanup omitted the provision-only project's residual network; only that exact recorded owner project was then removed.

The `0700` directory risk was found by inspection, but did not cause a Desktop runtime refusal because that bind was mapped to UID 101. The independent Linux DAC proof establishes the corrected behavior without claiming otherwise. These are fixture/observation corrections within the selected architecture; no stack or transport approach was reversed.

## Evidence retention and boundaries

Private native artifacts are under the external evidence block `infrastructure-acceptance-20261118-ibjg5ysk/platform-ui-network-20261119`, outside Git. Consistent owner-local SQLite exports, fixture identities and credentials were archived: **106 files**, byte-verified, archive mode `0600`; extracted private roots were removed. All disposable containers/volumes and the operation-only network were removed: inner Engine ends with **0 containers, 0 volumes, 3 default networks**. Own Engine/watch are stopped; the lowest sampled host free space was **22.38 GiB**. All **160 original outer container IDs** remain; the **159 other containers** preserve name, image, running/paused state, start time and restart count.

This result does not claim browser-driven login, replicated API writers, production SPIRE identity delivery, full installer startup, distributed backup/restore, provider admission, final security/independent verification, clean-checkout release or canonical deployment switching. Physical multi-computer proof remains TODO-MVP by user decision. No canonical RESUME, queue processing or OpenAI call occurred. The next approved implementation block is Scope/Deep ASGI alignment, after its own history and conflict review.

Official algorithms and the internal contracts supporting these choices are linked in the [plan](network-profile-plan-20261119.md#official-and-internal-grounds); operator inputs and role-secret boundaries are in [PROVIDER_OPERATIONS](../PROVIDER_OPERATIONS.md#независимое-размещение-ui-api-и-издателя).
