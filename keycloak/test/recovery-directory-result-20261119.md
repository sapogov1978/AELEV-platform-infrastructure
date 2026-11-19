# Keycloak recovery directory repair — 2026-11-19

Component result: PASS. All six platform outcomes remain open.

The actual current Compose recipe failed the new mapping regression before implementation (`incomplete Keycloak recovery source map`). The corrected mapper accepts the directory bind and retains the old two-file mapping under backup schema 1. It rejects duplicate/ambiguous certificate declarations.

Restore preflights existing file collisions and unsafe mount roots before creating credentials. New directory roots receive `0750` and their declared runtime group; existing roots are validated without changing ownership or mode. Runtime TLS/password files receive `0640` and the Keycloak secret group even under umask `0077`. The one-shot provisioner receives this group separately from its status primary group. Private archive directories/files remain `0700/0600`. This completes the existing 2026-11-07 runtime-permission contract for restore; the bootstrap fix is reused, not repeated.

## Verification and exact scope

- Host Python 3.13.16: **78 Keycloak tests passed**, including actual Compose rendering, legacy mapping, restore call ordering and refusal before credentials on unsafe roots, existing files or aliased paths.
- Ruff passes for the complete Keycloak directory. Black passes for the three changed Python files. Mypy strict mode passes for those three explicit files with `--follow-imports=silent`. A preliminary unrestricted import traversal exposed nine pre-existing typing errors in unchanged `test_configuration.py`; those are not reported as fixed or silently included in the clean typing scope.
- Native Linux/Python **3.13.7**: **13 permission checks passed** in an isolated, network-disabled, read-only disposable container, with a tmpfs and only CHOWN/DAC_OVERRIDE/SETUID/SETGID capabilities. Old directory/password behavior denies access; restored files are readable by runtime UID 1000, status UID 101 with its additional group, and a provisioner with a different primary group. Foreign groups and runtime reads of the private archive are denied. No live credentials are used.
- Source files, native image and result hashes are in [the bindings](recovery-directory-bindings-20261119.json). Raw qualification SHA-256: `5d1a4b38f9d5d42c153ae1f0173aab7f64bec4b17395c5439c7bd8c61184393b`.
- An additional full native pytest attempt did **not** execute the suite: the general qualification operator image contains no pytest. This remains a setup failure, separate from the passing host suite and standalone native DAC probe. No packaged Keycloak test-stage claim is made.
- Complete catalogue regenerated: **94 roles — 57 runtime, 1 node, 36 operations**. No runtime image/dependency pin change. Before/after inventory of all **160 existing Docker containers** matches; both temporary containers were removed, the old isolated Engine remains stopped.

The existing SQL dump/replay and publisher backup algorithms were not changed or requalified by these permission checks. No fresh database restore/login, release admission, full distributed profile, installer or soak is claimed. The separately placed Keycloak/PostgreSQL TLS recipe remains the next unresolved prerequisite. Canonical runtime, identities, queues, pause state and recovery budgets were not switched.

## Plan and grounds

[Plan with conflict review and estimate](recovery-directory-plan-20261119.md); [owner specification](../SPEC.md); [recovery procedure](../USER_MANUAL.md#остановка-обновление-и-резервная-копия).

Official grounds: [Docker bind mounts](https://docs.docker.com/engine/storage/bind-mounts/), [Python directory creation](https://docs.python.org/3.13/library/pathlib.html#pathlib.Path.mkdir), [descriptor permissions](https://docs.python.org/3.13/library/os.html#os.fchmod), [Linux directory search and supplemental groups](https://man7.org/linux/man-pages/man7/path_resolution.7.html).
