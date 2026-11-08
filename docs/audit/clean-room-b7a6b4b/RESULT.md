# Clean-room full-local: PASS, 11/11

Проверенная программная версия: [`b7a6b4b9957ce36a97161f6064b4eb7245f118d4`](https://github.com/sapogov1978/AELEV-platform-infrastructure/commit/b7a6b4b9957ce36a97161f6064b4eb7245f118d4). Полный [run 42489907582](https://github.com/sapogov1978/AELEV-platform-infrastructure/actions/runs/42489907582) и все 26 jobs [repository CI 42487425210](https://github.com/sapogov1978/AELEV-platform-infrastructure/actions/runs/42487425210) завершились успешно. Gate работал 2026-11-08 с 15:30:57 до 16:02:37 Europe/Belgrade; `source_dirty=false`, clean baseline и сохраняющая cleanup подтверждены. Независимый verifier после дополнительного ужесточения проверки BOM также дал `PASS`, 11/11, без blocking codes.

## Проверяемый пакет

[evidence.tar.gz](evidence.tar.gz) сохраняет исходные байты BOM, evidence, verification и всех 13 уникальных функциональных артефактов, включая два снимка Chromium. Внутри также находятся неизменённые `full-local.json` и `release.json` этой версии для воспроизведения исторической проверки. Build/setup logs, env, secrets, credentials и рабочие данные в пакет не включены. Текстовые артефакты до упаковки прошли secret scan; исходные отчёты не редактировались. Относительные ссылки внутри исходных отчётов сохраняют контекст соответствующего владельца; актуальные руководства доступны через [общий runbook](../../operations/clean-room-reproduction.md).

| Объект | SHA-256 |
|---|---|
| `evidence.tar.gz` | `a51d0bc577ecdc5d060458663491a6176c0eea7c2c91dc9b6f2fc86c6af467c0` |
| `release-bom.json` | `e11bb2d5a8292073f692920cd66c00f6a4e403395159311fc984497978567c07` |
| `clean-room-evidence.json` | `af186af9d2378632d73745c0ff207be37be99ecae377ab5d0d827ed50e968b55` |

Первоначальный GitHub artifact: `clean-room-full-local-b7a6b4b9957ce36a97161f6064b4eb7245f118d4`, ID `13621779238`. Репозиторная копия сохраняет проверяемые байты независимо от срока хранения Actions artifact.

Из корня checkout этой версии проверка выполняется без запуска контейнеров:

```sh
proof_dir=$(mktemp -d)
tar -xzf docs/audit/clean-room-b7a6b4b/evidence.tar.gz -C "$proof_dir"
python3 test_infrastructure/clean_room.py \
  --evidence "$proof_dir/clean-room-evidence.json" \
  --bom "$proof_dir/release-bom.json"
```

Ожидаемый результат — `status=PASS`, `verified_functions=11`, `required_functions=11`, пустой `blocking_codes`. Регрессия дополнительно проверяет неизменность архива и его исторический manifest; будущая версия продукта не получает допуск из этого отчёта автоматически.

## Функции

| Функция | Результат |
|---|---|
| Реальный OIDC login | PASS |
| Все владельческие состояния в Platform UI | PASS |
| Kafka round trip | PASS |
| PostgreSQL transaction/HA/backup restore | PASS |
| MinIO object round trip и site replication | PASS |
| Public Egress default deny/exact allow | PASS |
| Telemetry delivery/query/Grafana и outage recovery | PASS |
| Translator queue/dedup/restart/Kafka outage без провайдера | PASS |
| SPIRE issue/rotate/revoke | PASS |
| SPIRE observation/durable delivery/recovery/Grafana | PASS |
| Bounded recovery с сохранением истории | PASS |

Отдельный [run 42487453085](https://github.com/sapogov1978/AELEV-platform-infrastructure/actions/runs/42487453085) до полной приёмки прошёл три PostgreSQL-сценария с потерей каждого узла: 1→2, 2→3, 3→1; все подтвердили данные, физическое восстановление и исходный baseline. [Run 42487455393](https://github.com/sapogov1978/AELEV-platform-infrastructure/actions/runs/42487455393) отдельно подтвердил telemetry и Translator. [Матрица исследования](../clean-room-completion-plan.md) сохраняет причины прежних отказов и пределы выводов.

## Область вывода

Подтверждён полный поддерживаемый локальный профиль 15 инфраструктурных владельцев и 55 корневых ролей. Реализация перевода не выбрана; compute/provider не входит в его обязательный состав. Конкретная production-площадка отдельно подтверждает PKI/KMS/SVID, DNS/firewall/WAN, независимые failure domains, capacity, независимый backup/PITR, RPO/RTO, подпись/admission и фактическую доставку alert по [аудиту](../production-readiness.md).
