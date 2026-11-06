#!/usr/bin/env python3
"""Build or verify the non-normative inherited ADR disposition index."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
EXTRACTION = ROOT / "docs/audit/extraction.json"

INCORPORATED: dict[str, tuple[str, ...]] = {
    "00-INFRA-20261106-0002-infrastructure-repository-layout": (
        "20261009-0094-flat-infrastructure-orchestration-files.md",
    ),
    "00-INFRA-20261106-0003-owner-boundaries-and-consumer-resources": (
        "20260727-0003-bounded-context-separation.md",
        "20260727-0006-data-ownership-and-projections.md",
        "20261010-0103-consumer-agnostic-infrastructure-orchestration.md",
    ),
    "00-INFRA-20261106-0004-self-contained-service-delivery": (
        "20261007-0082-self-contained-service-delivery.md",
        "20261008-0088-componentized-infrastructure-orchestration.md",
        "20261008-0089-compose-owned-local-networks.md",
        "20261008-0090-infrastructure-configuration-boundaries.md",
        "20261010-0104-owner-contained-compose-recipes.md",
        "20261012-0113-infrastructure-plan-stability.md",
        "20261015-0134-uniform-http-probes-and-owner-unique-aliases.md",
    ),
    "00-INFRA-20261106-0005-kafka-cloudevents-contracts": (
        "20260727-0001-kafka-only.md",
        "20260727-0005-event-contract-governance.md",
        "20261008-0091-service-owned-kafka-topics.md",
    ),
    "00-INFRA-20261106-0006-technical-storage-boundaries": (
        "20260825-0042-storage-by-workload-and-context.md",
        "20261008-0092-minio-technical-identity.md",
        "20261008-0093-minio-multi-site-topology.md",
        "20261009-0098-clustered-infrastructure-postgresql.md",
        "20261010-0106-selectable-postgresql-fencing.md",
        "20261012-0115-owner-local-embedded-sqlite.md",
    ),
    "00-INFRA-20261106-0007-workload-and-operator-identity": (
        "20260803-0017-zero-trust-and-operational-security.md",
        "20261011-0108-loopback-operator-http.md",
        "20261011-0111-owner-operator-access-modes.md",
        "20261012-0117-loopback-http-embedded-ui.md",
        "20261013-0119-unified-platform-operator-sso.md",
        "20261020-0154-spire-workload-identity-provider.md",
    ),
    "00-INFRA-20261106-0008-platform-ui-owner-injection": (
        "20261004-0073-pluggable-platform-ui.md",
        "20261009-0095-service-owned-standalone-ui.md",
        "20261009-0101-provider-injected-platform-ui.md",
        "20261009-0102-owner-controlled-shared-refresh-indicator.md",
        "20261010-0105-publisher-presence-and-graceful-withdrawal.md",
    ),
    "00-INFRA-20261106-0009-independent-observability-and-telemetry": (
        "20261009-0099-consumer-owned-observability.md",
        "20261009-0100-durable-owner-telemetry-delivery.md",
        "20261011-0107-independent-observability-products.md",
    ),
    "00-INFRA-20261106-0010-bounded-recovery-and-process-control": (
        "20261003-0072-owner-local-process-control.md",
        "20261018-0148-bounded-local-infrastructure-recovery.md",
        "20261023-0155-automatic-recovery-after-cooldown.md",
        "20261028-0171-recovery-resume-and-compose-handoff.md",
    ),
    "00-INFRA-20261106-0011-controlled-public-egress": (
        "20261004-0074-service-owned-egress-dependencies.md",
        "20261012-0114-infrastructure-public-egress-gateway.md",
    ),
    "00-INFRA-20261106-0012-translator-technical-capability": (
        "20261007-0086-independent-kafka-translator-service.md",
        "20261008-0087-infrastructure-translator-service.md",
        "20261009-0096-consumer-agnostic-scalable-translator.md",
        "20261009-0097-translator-kafka-dependency.md",
        "20261011-0109-operator-translation-through-kafka.md",
        "20261011-0110-translator-diagnostic-openapi-groups.md",
        "20261011-0112-index-translate-9b.md",
        "20261013-0124-provider-owned-translation-execution.md",
    ),
    "00-INFRA-20261106-0013-owner-certificate-lifecycle": (
        "20261027-0165-local-observation-certificate-lifecycle.md",
        "20261027-0166-owner-declared-certificate-lifecycle.md",
    ),
}


def render() -> str:
    extraction = json.loads(EXTRACTION.read_text(encoding="utf-8"))
    archive = ROOT / extraction["archive_root"]
    disposition = {
        filename: target
        for target, filenames in INCORPORATED.items()
        for filename in filenames
    }
    duplicate_names = len(disposition) != sum(map(len, INCORPORATED.values()))
    if duplicate_names:
        raise ValueError("an inherited ADR has more than one canonical disposition")
    lines = [
        "# Архив унаследованных решений",
        "",
        "Эти файлы сохранены побайтно как provenance выделения Platform Infrastructure и не являются нормативными ADR этого репозитория. Исходные identity и SHA-256 находятся в [`extraction.json`](../extraction.json). Инфраструктурные инварианты переписаны в короткий набор `00-INFRA-*`; предметные решения потребителя исключены из нормативного набора без удаления истории.",
        "",
        "| Исходное решение | Исходный статус | Disposition |",
        "|---|---|---|",
    ]
    seen: set[str] = set()
    for source_name in extraction["inherited_adrs"]:
        relative = Path(source_name).relative_to("docs/adr")
        path = archive / relative
        title = path.read_text(encoding="utf-8").splitlines()[0].removeprefix("# ")
        filename = path.name
        seen.add(filename)
        source_status = "obsolete" if "obsolete" in relative.parts else "active в исходном репозитории"
        target = disposition.get(filename)
        if target is None:
            outcome = "Предметное/consumer-решение; не нормативно для Platform Infrastructure."
        else:
            outcome = (
                f"Инфраструктурный инвариант переписан в "
                f"[{target}](../../adr/{target}.md); исходный текст не нормативен."
            )
        lines.append(f"| [{title}]({relative.as_posix()}) | {source_status} | {outcome} |")
    unknown = sorted(set(disposition) - seen)
    if unknown:
        raise ValueError(f"disposition references unknown inherited ADRs: {unknown}")
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    arguments = parser.parse_args()
    output = ROOT / "docs/audit/inherited-adrs/INDEX.md"
    expected = render()
    if arguments.check:
        if not output.is_file() or output.read_text(encoding="utf-8") != expected:
            raise SystemExit("Inherited ADR disposition index is stale")
        print("Inherited ADR disposition index passed")
        return 0
    output.write_text(expected, encoding="utf-8")
    print(f"Wrote {output.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
