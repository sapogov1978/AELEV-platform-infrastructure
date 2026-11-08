# Clean-room full-local: PASS исправленной версии, 11/11

Проверенная программная версия: [`b1e2ee164f8e9a94ce248c64233d808d43771a78`](https://github.com/sapogov1978/AELEV-platform-infrastructure/commit/b1e2ee164f8e9a94ce248c64233d808d43771a78). Полный [run 42524864122](https://github.com/sapogov1978/AELEV-platform-infrastructure/actions/runs/42524864122) и все 26 jobs [CI 42522878437](https://github.com/sapogov1978/AELEV-platform-infrastructure/actions/runs/42522878437) завершились успешно. Gate работал 2026-11-08 с 22:24:30 до 22:59:46 Europe/Belgrade. Подтверждены все 11 функций, `source_dirty=false`, clean baseline и сохраняющая cleanup. Независимый verifier и повторная проверка распакованного репозиторного архива дали `PASS`, 11/11, без blocking codes.

## Проверяемый пакет

[evidence.tar.gz](evidence.tar.gz) содержит неизменённые BOM, evidence, исходный verification, независимый verification, все 13 уникальных функциональных артефактов и точные `full-local.json`/`release.json` проверенной версии. Build/setup logs, env, credentials, приватные ключи и рабочие данные не включены. Текстовые артефакты прошли secret scan; исходные отчёты не редактировались. Их относительные ссылки сохраняют прежний владельческий контекст, поэтому отчёты хранятся внутри архива; актуальная документация доступна через [единый runbook](../../operations/clean-room-reproduction.md).

| Объект | SHA-256 |
|---|---|
| `evidence.tar.gz` | `e8c563a1e7e69ac07de7003d87b3290197fe90d881e201894ab88877313e21a0` |
| `investigation.tar.gz` | `0f309cd649634925c43b672f86e0e4677fe6cace09f9bba33575e252550db9e2` |
| `release-bom.json` | `193e5da05fe55ac337ddd0d2fd126cbc68a984479835148d5f2695d2a54c4733` |
| `clean-room-evidence.json` | `c1654b5dc11947cbe6f17eaa563b7328d4e7b36f0cac2649a8adf6a6714048b7` |

Первоначальный GitHub artifact: `clean-room-full-local-b1e2ee164f8e9a94ce248c64233d808d43771a78`, ID `13636590123`. Репозиторная копия сохраняет проверяемые байты независимо от срока хранения Actions artifact. Проверка из корня репозитория, содержащего этот пакет:

```sh
proof_dir=$(mktemp -d)
tar -xzf docs/audit/clean-room-b1e2ee1/evidence.tar.gz -C "$proof_dir"
python3 test_infrastructure/clean_room.py \
  --evidence "$proof_dir/clean-room-evidence.json" \
  --bom "$proof_dir/release-bom.json"
```

Ожидается `status=PASS`, `verified_functions=11`, `required_functions=11`, пустой `blocking_codes`. Для исторического пакета после смены release manifest используется `clean_room.verify(evidence_path, bom_path, root=proof_dir)` с сохранёнными manifest из архива. Документальный commit с этим пакетом не является новым runtime-выпуском: допуск относится к указанному source commit. Следующий runtime-выпуск требует собственного CI, BOM и полного gate.

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

## Причина прежнего цикла и адресная проверка

В [run 42500787300](https://github.com/sapogov1978/AELEV-platform-infrastructure/actions/runs/42500787300) первоначальный ведущий 1 отказал в двух из трёх попыток. Последовательные обращения к недоступному первому DCS endpoint задерживали повторные проверки аренды: между `terminated` и `confirmed`/`failed` стабильно проходило 10,038–10,054 секунды. Это интервал нескольких проверок, а не измерение одного HTTP-запроса. Клиент переставал ждать, native-аренда освобождалась, второй peer получал `NO_LEADER_LEASE`; guarded return работал, и Patroni повторял цикл. Случайные прогоны и предварительный switchover не гарантировали это исходное состояние. Сравнение прежнего и исправленного кода с зависшим первым endpoint воспроизвело RED/GREEN.

Исправлен только транспорт чтения: свежие linearizable запросы выбирают доступный endpoint в прежнем native retry budget, с одним сетевым запросом на endpoint, без кеша и переиспользования запоздавшего ответа. Для возврата вторая свежая проверка начинается после первой и требует другого endpoint/member, того же cluster, неубывающей revision и одинаковой аренды/её отсутствия. Таймеры Patroni, TLS, quorum, fences и бюджеты возврата сохранены. Отдельно общий test driver перестал считать штатную временную остановку собственного SQL peer немедленным terminal failure; он ждёт настоящую готовность до прежнего deadline при здоровом Docker-fencing исполнителе и без OOM, не отправляя restart сам.

После исправления [run 42522896912](https://github.com/sapogov1978/AELEV-platform-infrastructure/actions/runs/42522896912) подтвердил три полных цикла первоначального ведущего 1 без switchover: 1→3, 1→3, 1→2. [Run 42522896648](https://github.com/sapogov1978/AELEV-platform-infrastructure/actions/runs/42522896648) подтвердил root/Kafka и затем тот же initial-1 сценарий. Везде сохранены source/Docker baseline и cleanup. [investigation.tar.gz](investigation.tar.gz) сохраняет 17 безопасных исходных/расчётных файлов до и после исправления, включая обе неудачи и прежний успешный повтор; выборка не скрывает непостоянство дефекта.

## Область вывода

`PRODUCTION_READY` относится к поддерживаемому `full-local` продукту из 15 инфраструктурных владельцев. Комплект runbook, manifests, API, preflight/BOM и исполняемых acceptance-проверок подтверждает функциональное воспроизведение из чистого состояния. Реализация перевода не выбрана: Translator использует непрозрачный порт, compute/provider не входит в обязательную композицию. PKI/KMS/SVID, DNS/firewall/WAN, независимые failure domains, capacity, независимый backup/PITR, RPO/RTO, supply-chain admission и доставка alert конкретной площадки закрываются отдельно по [аудиту](../production-readiness.md). [Первый полный PASS](../clean-room-b7a6b4b/RESULT.md) и вся прежняя история сохранены; основание текущего допуска — исправленная версия выше.
