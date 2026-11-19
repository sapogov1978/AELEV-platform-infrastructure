# Keycloak/PostgreSQL independently placed TLS profile — 2026-11-19

Status: selected scope before implementation, not qualification or release admission.

## Reuse and history

Current `keycloak/compose.yml` hardcodes `keycloak-database:5432`, does not publish the SQL/management listeners and assumes a local management network. Full path history contains no independently placed JDBC/TLS recipe. Former full-local `2d597d8` and 2026-10-14 restore proofs remain bound to their original local topology. The directory-recovery correction is already completed in `1ae4103`; do not repeat it. SPIRE bootstrap and owner OIDC client provisioning also already exist and must be reused.

## Intended result and estimate

Estimate announced before edits: **60–90 minutes**, including negative/native checks; the existing daily review deadline is not extended by this estimate.

1. Add an explicit owner network overlay and sample non-secret inputs. Preserve base recipe, images, PostgreSQL 17.6, Keycloak 26.3.2, bundled pgJDBC 42.7.7 and Python/OS/SQLite pins. SQL and HTTPS management listeners have explicit published bind/ports. Keycloak connects only to the declared reachable SQL hostname/port using `verify-full` and its own client PKCS12 identity. Network connections require mTLS plus the existing SCRAM password; plaintext is rejected.
2. Supply one owner operation, `keycloak-database-identity-preparer`, using the existing pinned SQL image and its shell tools. It copies exactly three declared server inputs into that SQL instance's private volume with the pinned PostgreSQL UID/GID 70:70 and key mode `0600`; replay verifies unchanged bytes/metadata, drift does not overwrite. This avoids relying on supplementary groups surviving the image's root-to-postgres transition. No server key is shared with Keycloak.
3. Include the new SQL server/client TLS inputs in schema-1 backup and fresh-target restore, reject selected-profile mismatch or a missing network identity before writing credentials, and preserve the optional current HTTPS public CA. SQL dump/publisher snapshot algorithms remain owner procedures. Existing local recovery does not establish remote multi-node orchestration; the full installer must execute/collect the owner operations on their declared placements before the global recovery gate can close.
4. Verify real Keycloak and its SQL role in separate Compose instances/networks using a published endpoint; real OIDC discovery/readiness, database TLS identity and negative CA/hostname/client-certificate/plaintext checks; dump/restore into fresh local rehearsal targets and exact TLS material. No canonical switch/RESUME, consumer policy or OpenAI call. Keep component vs installer/physical gates separate.

Packaging check: the checks stage must contain all current recipe inputs. A native packaged run exposed the former missing `compose.local-bridge.yml` COPY (81 tests passed, two old configuration tests failed). Before correction an additional estimate of 5–10 minutes was announced. Add the missing legacy file only to checks, preserve the failed evidence, and rerun packaged tests; runtime layers and the JDBC/SQL behavior are unchanged by this packaging correction.

## Conflict review before implementation

- **Storage ownership:** PostgreSQL remains Keycloak-owned canonical storage. The general PostgreSQL capability is not substituted, and no functional consumer reads identity storage.
- **Transport:** published SQL is infrastructure transport; Kafka-only domain transport remains unchanged. Explicit JDBC endpoint/CA/hostname are mandatory; no Docker-name or plaintext fallback.
- **Placement/resources:** native qualification must give each role a distinct Compose instance, network names and subnet; role startup uses `--no-deps`. Do not claim isolation from merely publishing ports while keeping a required shared bridge. Final all-role workflow/placement selection remains an installer gate.
- **Stack:** upstream Keycloak 26.3.2 POM explicitly declares PostgreSQL 17 and driver 42.7.7. Use its built-in driver, not a new dependency. PKCS12 alias is `user`, `.p12` with an explicitly empty password; the file itself remains a protected runtime private key, like an unencrypted PEM. Server and client keys remain separate.
- **Filesystem:** PostgreSQL checks server-key owner/mode; prepare into its own volume rather than assuming host UID equivalence. Entire runtime identity directories are mounted, preserving owner-controlled atomic replacement behavior. Idempotent prepare refuses drift, so active rotation remains a controlled owner handoff.
- **Readiness:** management HTTPS has its own reachable endpoint; publisher probes its existing `/health/ready` contract. An open 8443 port is not a full readiness result. Keep readiness and process/publisher signals distinct.
- **Recovery:** selected network profile must not silently restore through legacy plaintext. New credentials must be in the owner backup. A local full-bundle rehearsal is distinct from a distributed recovery coordinator and from the old proof. Preserve stopped-target, same realm/UUID, fresh SQL/publisher volumes, no-overwrite and budget gates.
- **Catalogue:** add exactly one operation; permanent runtime role count remains 57. Keep the full-local manifest/default bootstrap unchanged until the complete workflow explicitly selects the new profile.

No unresolved incompatible approach remains for this component scope. If three alternative approaches cannot meet these combined constraints, stop dependent edits and interview the owner.

## Internal and official grounds

- [Owner SPEC](../SPEC.md), [manual](../USER_MANUAL.md), [ADR 0004](../../docs/adr/00-INFRA-20261106-0004-self-contained-service-delivery.md), [ADR 0007](../../docs/adr/00-INFRA-20261106-0007-workload-and-operator-identity.md), [ADR 0018](../../docs/adr/00-INFRA-20261113-0018-independent-roles-and-translation-mtls.md), [placement API](../../docs/api/placement.md).
- [Keycloak 26.3.2 POM](https://github.com/keycloak/keycloak/blob/26.3.2/pom.xml): PostgreSQL 17 and driver 42.7.7 compatibility.
- [Keycloak 26.3.2 health](https://github.com/keycloak/keycloak/blob/26.3.2/docs/guides/observability/health.adoc), [management interface](https://github.com/keycloak/keycloak/blob/26.3.2/docs/guides/server/management-interface.adoc): management readiness listener and TLS inheritance.
- [pgJDBC SSL](https://jdbc.postgresql.org/documentation/ssl/), [42.7.7 PKCS12 key manager](https://github.com/pgjdbc/pgjdbc/blob/REL42.7.7/pgjdbc/src/main/java/org/postgresql/ssl/PKCS12KeyManager.java): hostname/CA verification, PKCS12 alias and password handling.
- [PostgreSQL 17 TLS](https://www.postgresql.org/docs/17/ssl-tcp.html), [host authentication rules](https://www.postgresql.org/docs/17/auth-pg-hba-conf.html): key ownership, client certificate CN and first-match hostssl/no-fallback rules.
- [Compose up](https://docs.docker.com/reference/cli/docker/compose/up/), [Compose networks](https://docs.docker.com/reference/compose-file/networks/), [bind mounts](https://docs.docker.com/engine/storage/bind-mounts/): explicit instance resources, no-deps role starts and protected owner inputs.
