#!/usr/bin/env python3
"""Validate owner documentation, diagram coverage, and official references."""

from __future__ import annotations

import re
from pathlib import Path


OWNER_DIRS = (
    "kafka",
    "keycloak",
    "minio",
    "postgresql",
    "public-egress-gateway",
    "spire",
    "platform-ui",
    "translator-service",
    "observability/delivery",
    "observability/otel-collector",
    "observability/prometheus",
    "observability/blackbox-exporter",
    "observability/loki",
    "observability/tempo",
    "observability/grafana",
)

SERVICE_MANUALS = (
    "USER_MANUAL.md",
    "kafka/USER_MANUAL.md",
    "keycloak/USER_MANUAL.md",
    "keycloak/secrets/USER_MANUAL.md",
    "minio/USER_MANUAL.md",
    "minio/secrets/USER_MANUAL.md",
    "observability/USER_MANUAL.md",
    "observability/blackbox-exporter/USER_MANUAL.md",
    "observability/delivery/USER_MANUAL.md",
    "observability/grafana/USER_MANUAL.md",
    "observability/loki/USER_MANUAL.md",
    "observability/otel-collector/USER_MANUAL.md",
    "observability/prometheus/USER_MANUAL.md",
    "observability/tempo/USER_MANUAL.md",
    "platform-ui/USER_MANUAL.md",
    "platform-ui/client/USER_MANUAL.md",
    "postgresql/USER_MANUAL.md",
    "postgresql/secrets/USER_MANUAL.md",
    "public-egress-gateway/USER_MANUAL.md",
    "public-egress-gateway/secrets/USER_MANUAL.md",
    "spire/USER_MANUAL.md",
    "test_infrastructure/USER_MANUAL.md",
    "translator-service/USER_MANUAL.md",
    "translator-service/secrets/USER_MANUAL.md",
    "translator-service/test/model_comparison/USER_MANUAL.md",
)

INFRA_ADR_PATTERN = re.compile(
    r"00-INFRA-(?P<date>\d{8})-(?P<number>\d{4})-(?P<essence>[a-z0-9]+(?:-[a-z0-9]+)*)\.md"
)
REQUIRED_INFRA_ADR_SECTIONS = (
    "## Контекст",
    "## Ключевые решения",
    "## Последствия",
    "## Проверка и откат",
    "## Официальные основания",
)
FORBIDDEN_INFRA_ADR_TERMS = (
    "advance-screening",
    "base-screening",
    "deep-screening",
    "crawler",
    "discovery-engine",
    "research-management",
    "sensor-surface",
)


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    errors: list[str] = []
    audit = (root / "docs/audit/production-readiness.md").read_text()
    adr_index = (root / "docs/adr/INDEX.md").read_text()
    official_sources = (root / "docs/references/official-sources.md").read_text()

    for relative in (
        "full-local.json",
        "tools/full_local.py",
        "docs/operations/clean-room-reproduction.md",
        "docs/schemas/clean-room-evidence-v1.json",
        "test_infrastructure/clean_room_gate.py",
        "test_infrastructure/clean_room.py",
    ):
        if not (root / relative).is_file():
            errors.append(f"Missing clean-room contract: {relative}")
    for relative in (
        "README.md",
        "SPEC.md",
        "INDEX.md",
        "TASKS.md",
        "docs/api/installation.md",
        "docs/audit/production-readiness.md",
    ):
        if "clean-room" not in (root / relative).read_text().lower():
            errors.append(f"Clean-room contract is not linked: {relative}")

    for owner in OWNER_DIRS:
        directory = root / owner
        for name in (
            "README.md",
            "SPEC.md",
            "USER_MANUAL.md",
            "INDEX.md",
            "architecture.mmd",
        ):
            if not (directory / name).is_file():
                errors.append(f"Missing owner document: {owner}/{name}")
        readme = (directory / "README.md").read_text()
        if "architecture.mmd" not in readme:
            errors.append(f"Owner README does not link its diagram: {owner}")
        if "[INDEX.md](INDEX.md)" not in readme:
            errors.append(f"Owner README does not link its agent map: {owner}")
        diagram = (directory / "architecture.mmd").read_text()
        if len(re.findall(r"(?:-->|<-->|-.->|==>)", diagram)) < 3:
            errors.append(f"Owner diagram has fewer than three relationships: {owner}")
        if f"../../{owner}/architecture.mmd" not in audit:
            errors.append(f"Owner missing from readiness audit: {owner}")

    agent_indexes = (
        root / "INDEX.md",
        root / "observability/INDEX.md",
        *(root / owner / "INDEX.md" for owner in OWNER_DIRS),
    )
    for path in agent_indexes:
        if not path.is_file():
            errors.append(f"Missing agent navigation map: {path.relative_to(root)}")
            continue
        text = path.read_text()
        if "агентская карта" not in text.lower():
            errors.append(f"Agent map does not declare its purpose: {path.relative_to(root)}")
        if "| Что искать | Где находится |" not in text:
            errors.append(f"Agent map is not tabular: {path.relative_to(root)}")

    for path in root.rglob("*"):
        if not path.is_file() or path.name != "index.md":
            continue
        relative = path.relative_to(root)
        if relative.parts[0] in {".git", "graphify-out"}:
            continue
        if relative.parts[:2] in {("docs", "audit"), ("docs", "obsolete")}:
            continue
        errors.append(f"Active agent map must be uppercase INDEX.md: {relative}")

    for relative in SERVICE_MANUALS:
        path = root / relative
        if not path.is_file():
            errors.append(f"Missing service manual: {relative}")
            continue
        text = path.read_text()
        heading = "## Официальные руководства"
        if heading not in text:
            errors.append(f"Official guidance section missing: {relative}")
            continue
        section = text.split(heading, 1)[1]
        if not re.search(r"\[[^]]+\]\(https://[^)]+\)", section):
            errors.append(f"Official guidance has no external link: {relative}")

    infrastructure_adrs = sorted((root / "docs/adr").glob("00-INFRA-*.md"))
    if not infrastructure_adrs:
        errors.append("No repository-owned 00-INFRA ADRs found")
    for path in infrastructure_adrs:
        match = INFRA_ADR_PATTERN.fullmatch(path.name)
        if match is None:
            errors.append(f"Infrastructure ADR name violates convention: {path.name}")
            continue
        text = path.read_text()
        identifier = path.stem
        if not text.startswith(f"# {identifier} — "):
            errors.append(f"Infrastructure ADR title does not match filename: {path.name}")
        for heading in REQUIRED_INFRA_ADR_SECTIONS:
            if heading not in text:
                errors.append(f"Infrastructure ADR lacks {heading}: {path.name}")
        official_section = text.split("## Официальные основания", 1)[-1]
        if len(re.findall(r"https://[^)]+", official_section)) < 2:
            errors.append(
                f"Infrastructure ADR has fewer than two official sources: {path.name}"
            )
        key_section = text.split("## Ключевые решения", 1)[-1].split("\n## ", 1)[0]
        if len(re.findall(r"(?m)^- ", key_section)) < 2:
            errors.append(f"Infrastructure ADR has no key-decision list: {path.name}")
        if re.search(r"(?mi)^## (?:история|изменения|changelog)\b", text):
            errors.append(f"Infrastructure ADR contains change-log history: {path.name}")
        lowered = text.lower()
        for term in FORBIDDEN_INFRA_ADR_TERMS:
            if term in lowered:
                errors.append(
                    f"Consumer-domain term {term!r} in infrastructure ADR: {path.name}"
                )
        if f"]({path.name})" not in adr_index:
            errors.append(f"Infrastructure ADR missing from decision index: {path.name}")
        if identifier not in official_sources:
            errors.append(
                f"Infrastructure ADR missing from official-source registry: {path.name}"
            )

    unexpected_adr_documents = sorted(
        path.name
        for path in (root / "docs/adr").glob("*.md")
        if path.name != "INDEX.md" and INFRA_ADR_PATTERN.fullmatch(path.name) is None
    )
    if unexpected_adr_documents:
        errors.append(
            f"Non-infrastructure documents in normative ADR directory: {unexpected_adr_documents}"
        )

    if "Это не хронология изменений" not in adr_index:
        errors.append("ADR index does not separate decisions from change history")
    if re.search(r"(?m)^- \[ADR-\d+", adr_index):
        errors.append("ADR index contains a chronological inherited ADR list")

    archive_index = root / "docs/audit/inherited-adrs/INDEX.md"
    if not archive_index.is_file():
        errors.append("Inherited ADR disposition index is missing")
    elif len(re.findall(r"(?m)^\| \[ADR-", archive_index.read_text())) != 132:
        errors.append("Inherited ADR disposition index does not cover all 132 decisions")

    compatibility_paths = (
        root / "docs/audit/inherited-adrs/20261105-0001-independent-infrastructure-product.md",
        root / "docs/audit/inherited-adrs/20261106-0002-infrastructure-repository-layout.md",
    )
    for path in compatibility_paths:
        text = path.read_text()
        if "Этот файл не является ADR" not in text or "00-INFRA-" not in text:
            errors.append(f"Invalid legacy ADR compatibility redirect: {path.name}")

    translator_adr = (
        root / "docs/adr/00-INFRA-20261106-0012-translator-technical-capability.md"
    ).read_text()
    for forbidden in (
        "владельцем исполнения перевода, модели",
        "единственным владельцем своего model runtime",
    ):
        if forbidden in translator_adr:
            errors.append(f"Translator ADR assigns provider ownership: {forbidden}")
    for relative in (
        "translator-service/src/translator_service/runtime.py",
        "translator-service/src/translator_service/protocol.py",
        "translator-service/src/translator_service/provider_healthcheck.py",
    ):
        text = (root / relative).read_text()
        for forbidden in (
            "translator_ref",
            "read_translator_lock",
            "AELEV_TRANSLATOR_LOCK_PATH",
            "index_translation_provider",
        ):
            if forbidden in text:
                errors.append(
                    f"Translator core knows provider implementation ({forbidden}): "
                    f"{relative}"
                )

    translator_dockerfile = (root / "translator-service/Dockerfile").read_text()
    core_stage = translator_dockerfile.split(
        "FROM python-bookworm AS translator-core-runtime", 1
    )[-1].split(
        "FROM ${AELEV_INFRASTRUCTURE_TRANSLATOR_HEALTH_SEED_IMAGE}", 1
    )[0]
    for forbidden in (
        "COPY translator.lock.json",
        "COPY --from=index-model-assets",
        "COPY --from=index-runtime-builder",
        "AELEV_TRANSLATOR_MODEL_ROOT",
        "AELEV_TRANSLATOR_LOCK_PATH",
        "AELEV_TRANSLATOR_RUNTIME_PATH",
    ):
        if forbidden in core_stage:
            errors.append(f"Translator core image contains provider artifact: {forbidden}")
    compute_compose = (root / "translator-service/compose.compute.yml").read_text()
    for required in (
        "AELEV_INFRASTRUCTURE_TRANSLATOR_PROVIDER_IMAGE",
        "AELEV_INFRASTRUCTURE_TRANSLATOR_WORKER_IMAGE",
    ):
        if required not in compute_compose:
            errors.append(f"Translator compute split is missing {required}")
    if "AELEV_INFRASTRUCTURE_TRANSLATOR_COMPUTE_IMAGE" in compute_compose:
        errors.append("Translator provider and worker still share one image selector")
    provider_service = compute_compose.split("  translation-provider:", 1)[-1].split(
        "\n  translator-worker:", 1
    )[0]
    for forbidden in ("command:", "entrypoint:", "environment:", "healthcheck:"):
        if forbidden in provider_service:
            errors.append(
                f"Translator overrides opaque provider image contract: {forbidden}"
            )
    for relative in (
        "translator-service/translator.lock.json",
        "translator-service/download-model.py",
        "translator-service/src/translator_service/index_translation_provider.py",
        "translator-service/src/translator_service/index_languages.py",
    ):
        if (root / relative).exists():
            errors.append(f"Translator still owns provider implementation: {relative}")

    if errors:
        raise SystemExit("\n".join(errors))
    print(
        "Documentation audit passed: "
        f"{len(OWNER_DIRS)} owners, {len(SERVICE_MANUALS)} manuals, "
        f"{len(infrastructure_adrs)} repository-owned ADRs follow 00-INFRA convention; "
        "132 inherited decisions are audit-only"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
