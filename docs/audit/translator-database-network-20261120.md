# Canonical Translator SQL: native qualification, 20 November 2026

Result: `NATIVE_TRANSLATOR_DATABASE_COMPONENT_PASS`. The exact source bindings, commands, retained attempts and limits are recorded in the [machine-readable report](translator-database-network-20261120.json). This closes the canonical Translator SQL network/identity preparation component gap identified in the [pre-edit plan](../../translator-service/database-network-plan-20261120.md); it does not establish full platform startup or production readiness.

## History and unchanged scope

The 17 November queue proof already verified SQL/Kafka/provider behavior using a fixture-created TLS database. This qualification exercises the new canonical `compose.database-network.yml` through the existing owner prepare/task/start workflow. The tested source is Infrastructure `58baa8b42969effb3e8110e845f0e8e8a3ee8f6a`, with the sealed DRP `92ea71856f24c77bad26220dce35fbb7ebc78deb` controller. The source candidates remain unpublished and unadmitted. SQL image pins, application code/schema, legacy recipe, credentials, queues and accepted stack are unchanged.

Forecast before the native step was 30–45 minutes. Owned Engine activation through final archive and stop took about 15 minutes; report, narrow repository checks and delivery were forecast separately at 10–15 minutes. Three failed harness attempts are retained without rewriting their results. They required test-observation and fixture-cleanup corrections, with no intervening product changes.

## Verified behavior

- The existing owner SDK prepares the private workspace, runs the canonical identity operation, starts the pinned SQL container and observes real Docker health. SQL server and client use disjoint Docker networks; the client connects through the published node port with `verify-full`, a client certificate and SCRAM password.
- All seven negative cases reject access: wrong hostname, wrong CA, missing certificate, foreign CA, wrong certificate CN, wrong password and plaintext. For a valid-CA certificate with a foreign CN, PostgreSQL's public client error is generalized as password authentication failure. The test also requires the exact server-side expected/presented-name and `clientcert=verify-full` CN-mismatch records; password failure alone cannot pass this case.
- Workflow repeat preserves the original task UUID and target receipt, stdout/stderr bytes, ownership, modes and timestamps. Eleven prior target receipts remain exact; five additional receipts are bound to explicitly read-only plan/status operations. The running SQL container retains its incarnation on repeat.
- An actual SQL restart preserves the inserted row and its hash. A SQL dump is retained. Restore, replica failover and power-loss durability are outside this component result.
- Both Translator and Keycloak identity helpers pass 22 native cases in total: fresh/repeat, actual UID access and foreign UID/bootstrap denial, preexisting staging, invalid source, partial/foreign target, stale lock, and HUP/INT/TERM/KILL. Actual pinned-image PostgreSQL UID/GID is `70:70`, with delivered directory/file modes `0700/0600`. Only the signal test injects a readonly delayed `cp` wrapper; UID, filesystem metadata and access decisions are real Linux observations. SIGKILL leaves unknown staging/lock state, and the next invocation refuses repair.

The separate host phase has 47 existing/new owner checks plus actual before/after Compose normalization, syntax/style and type checks. Host UID/stat/sync adapters are explicitly distinguished from the native permission proof.

## Retained failures and cleanup

Attempt 1 used an over-specific client error label for the foreign-CN case. Attempt 2 read target receipt fields from the public owner result projection rather than the persisted SDK journal. Attempt 3 passed the functional checks but left eight anonymous image-declared volumes from detached signal fixtures; its original baseline failure remains recorded. The manager removed those eight only after matching captured own-container IDs, labels, mounts and Docker anonymous-volume metadata. The final harness uses ownership-checked `docker rm --volumes` for these exact fixtures. No prune or foreign cleanup occurred.

Attempt 4 passes the complete native proof and preserves the original inner Engine baseline. Its private archive contains **1,384 files / 32,673,792 bytes**, SHA-256 `11be056830cb3a73ede110078906ca21d8e23c9011be1707393889077d321b09`; every regular file's bytes, UID, GID and mode match the archived Linux evidence before controller-volume deletion. The owned Engine and disk watcher are stopped. All **161 other outer containers** retain their IDs, running/paused state, restart counts and start timestamps; minimum free space was **38.88 GiB**, above the 15 GiB floor. Temporary runtime containers, networks and volumes are removed; cached images are retained.

## Documentary grounds and remaining gates

The selected behavior follows [PostgreSQL server SSL/key permissions](https://www.postgresql.org/docs/17/ssl-tcp.html), [first-match HBA and clientcert authentication](https://www.postgresql.org/docs/17/auth-pg-hba-conf.html), [libpq verify-full](https://www.postgresql.org/docs/17/libpq-ssl.html), [Docker daemon-local volumes](https://docs.docker.com/engine/storage/volumes/), [container removal with anonymous volumes](https://docs.docker.com/reference/cli/docker/container/rm/) and [POSIX shell traps](https://pubs.opengroup.org/onlinepubs/9799919799/utilities/trap.html). Initial-delivery/no-repair policy and exact receipt preservation are AELEV decisions defined by [owner prepare](../api/owner-prepare.md), [owner Compose](../api/owner-compose.md), [ADR-0018](../adr/00-INFRA-20261113-0018-independent-roles-and-translation-mtls.md) and the owner plan.

Full 105-role configuration/startup, current provider quality and security admission, external identity lifecycle, UI/SSO/telemetry, integrated restore/soak, signed final releases and clean-checkout installation remain separate gates. No real provider credential, OpenAI request, canonical runtime switch or queue `RESUME` was used.
