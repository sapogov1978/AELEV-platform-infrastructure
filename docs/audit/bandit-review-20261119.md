# Exact Bandit review, 19 November 2026

Pre-edit estimate: **15–20 minutes** for both repositories, complete pinned scans and existing negative gates. SAST disposition only; broader security, image and production-ready admission remain open.

## History and selected change

The retained 188-file Bandit 1.8.6 scan in the [native workflow qualification](spire-native-workflow-20261119.md) has one unreviewed Medium: `keycloak/recovery.py|B103|c6f6f2af41818f718f43f15e9bbe5f2ab8799c5028000cec9370c24c41fadddf`. This file is unchanged by the SPIRE adapter. Git `1ae4103` introduced the current-recipe directory recovery repair; `696a190` extended the independently placed SQL identities. Neither added this exact SAST disposition. Original failed reports remain intact.

Admit only this fingerprint, count **1**. The operation opens a newly created directory with `O_DIRECTORY|O_NOFOLLOW`, assigns the declared runtime GID and sets `0750`. It validates directory type, operator UID, exact GID and mode. Existing incompatible roots and symlinks are refused without repair. There is no group write or other-user access. Group search/read is necessary to reach read-only mounts and `0640` credentials; this is not regular-file execute permission. Archive directories/files remain `0700/0600`.

The [prior native DAC proof](../../keycloak/test/recovery-directory-result-20261119.md) already verifies intended UID/GID access, denial without the group, foreign-group denial and denied runtime archive access. Reuse those exact historical bindings; no fresh DAC, SQL restore or login is claimed here. Rerun the existing host permission tests.

## Whole-scope conflict review

- Preserve [Keycloak SPEC](../../keycloak/SPEC.md) and [recovery plan](../../keycloak/test/recovery-directory-plan-20261119.md). Changing `0750` to `0700` would restore the proven access failure. No runtime, mount, dependency, credential, GID or recovery-budget change.
- Preserve scanner 1.8.6, full source scope, schema 2 file/rule/context fingerprint and exact count. No `nosec`, new exclusion or rule-wide allowance. Unknown/duplicate Medium, any High, analyzer errors and empty scans remain blockers.
- The consumer replaces two obsolete listener fingerprints after ASGI migration while preserving explicit publication, nonlocal HTTPS and OIDC. No internal-only topology, release or lock change.
- Historical qualification reports remain accurate about the failure at capture. Fresh SAST pass will not close other admission gates.

## Official grounds

[Bandit B103](https://bandit.readthedocs.io/en/1.8.6/plugins/b103_set_bad_file_permissions.html) flags permissive mode patterns requiring contextual review. [Linux path resolution](https://man7.org/linux/man-pages/man7/path_resolution.7.html) defines directory search and supplemental-group permissions. [OpenStack restrictive permissions](https://security.openstack.org/guidelines/dg_apply-restrictive-file-permissions.html) permits necessary owning-service/group access while excluding others and requires group review. [Python fchmod](https://docs.python.org/3.13/library/os.html#os.fchmod) and [Docker bind mounts](https://docs.docker.com/engine/storage/bind-mounts/) support the existing implementation, not runtime admission by themselves.

## Verification

**SAST PASS**: canonical checker and retained complete scan both analyzed **188 files / 45,964 lines**, zero High, 28 exact admitted Medium. The original baseline refuses the same scan on the one reviewed fingerprint. All **6** mutations of that finding are rejected: duplicate, High, changed context, changed path, analyzer error and empty scan. Existing Keycloak recovery tests: **15 passed**. The consumer's existing audit suite: **6 passed**. Runtime file bytes still match `3d8b24280b9e192454cc45d42288d978c24d5535`.

The [machine-readable result](bandit-review-20261119.json) binds raw report SHA-256 `c19704207ce1e7810660e2fbcf64822e4bd33e6dcd90b0ae42b8000e280ea7af` and exact baseline hash. Raw reports are retained in the private qualification evidence. This is a reviewed exception, not removal of B103 from the analyzer. No Docker/runtime/queue effects, fresh DAC or release claim. Broader security, image, full runtime/UI/E2E and release gates remain open.
