# Keycloak recovery: directory mounts, 2026-11-19

Status: plan before implementation; component repair, all six platform outcomes remain open.

## History and observed failure

`git log --all -- keycloak/recovery.py keycloak/test/test_recovery.py` identifies the original flattened delivery (`09ebda2`), with no later directory-mount repair. Current Compose binds `/run/secrets/keycloak-https`, but `source_map` recognizes only the two old `/opt/keycloak/conf/tls/*` file targets. Rendering the actual current recipe and calling the mapper fails with `incomplete Keycloak recovery source map`. Existing four recovery tests do not cover the current recipe.

Restore also creates a directory-mounted TLS root as operator-only `0700`, and creates the status root without assigning the declared runtime GID. File `0640` alone cannot give non-root runtime access through those directories. This must be repaired before introducing a separately placed TLS PostgreSQL endpoint; no network profile is implemented in this step.

## Planned change and time

Estimate announced before edits: 35–50 minutes including verification.

1. Recognize only the two fixed TLS leaves under the canonical directory bind, retaining legacy file mounts and backup schema 1. Reject ambiguous declarations.
2. Preflight restored files and directory roots before writing credentials. New mounted roots receive `0750` and the declared additional runtime GID; existing roots must already have compatible ownership/mode and cannot be symlinks. Do not silently change an existing directory. Explicit file modes must survive a restrictive operator umask. Restored runtime passwords and TLS files use the SPEC's `0640`/Keycloak secret GID, while archive directories/files stay `0700/0600`. The one-shot status provisioner additionally receives that same secret group so independently selected status/Keycloak GIDs cannot break its existing administrative-secret mount.
3. Run current-recipe and legacy mapping regressions, no-overwrite/unsafe-directory checks, full Keycloak checks, and a Linux process UID/GID permission probe. The probe uses disposable synthetic secret bytes, no live identity or domain work. Keep raw evidence outside Git.

## Whole-scope compatibility review

- [SPEC](../SPEC.md): exact owner files, no overwrite, same realm/UUID, stopped target and fresh SQL/publisher volumes remain required. SQL dump/replay and publisher backup APIs remain unchanged.
- [Manual](../USER_MANUAL.md#остановка-обновление-и-резервная-копия): schema 1 and encrypted external backup requirement remain unchanged; this repair makes the current recipe usable again.
- [ADR 0004](../../docs/adr/00-INFRA-20261106-0004-self-contained-service-delivery.md), [ADR 0007](../../docs/adr/00-INFRA-20261106-0007-workload-and-operator-identity.md), [ADR 0018](../../docs/adr/00-INFRA-20261113-0018-independent-roles-and-translation-mtls.md): local operator recovery is not a shared storage interface or proof of distributed placement. No dependency, image, role count, client permission, endpoint or recovery-budget change.
- Reject a directory/file mapping collision rather than selecting arbitrary certificate bytes. Validate all destination file collisions and existing root permissions before writing credentials. Preserve original archive privacy and reject root symlinks.
- Native permission results prove only this component's restored file access. They do not prove PostgreSQL restore, login, current release admission, full installer, soak or all six outcomes.

## Official grounds

- [Docker bind mounts](https://docs.docker.com/engine/storage/bind-mounts/): file and directory mounts expose the selected host filesystem objects; directory traversal permissions still apply.
- [Python 3.13 `Path.mkdir`](https://docs.python.org/3.13/library/pathlib.html#pathlib.Path.mkdir): mode is combined with umask, existing directories are not repaired by `exist_ok`.
- [Python 3.13 `os.fchmod` and `os.fchown`](https://docs.python.org/3.13/library/os.html#os.fchmod): explicit descriptor-based mode/group for newly created files/directories; no existing secret is replaced.
- [Linux pathname resolution](https://man7.org/linux/man-pages/man7/path_resolution.7.html): directory search permission and supplemental groups determine access before file read permission is applied.

Pre-completion scope check also identified the former restore-only `0600` runtime passwords, conflicting with the SPEC's declared `0640` runtime group access. Including this related repair was announced before its edits, with an additional estimate of about five minutes; the archive remains private and no working credentials are changed.

Three incompatible approaches without a selected compatible one require a user interview. Current selected approach preserves the recipe and archive contract; no unresolved architectural choice is required for this bounded repair.
