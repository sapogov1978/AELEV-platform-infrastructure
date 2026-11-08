# План завершения clean-room приёмки

Итог: исправленная программная версия `b1e2ee1` прошла initial-1 3/3 без switchover (`42522896912`), root/Kafka/initial-1 (`42522896648`), CI 26/26 и новый полный gate `42524864122`: PASS всех 11 функций 2026-11-08 в 22:59:46 Europe/Belgrade. Независимый verifier, повторная проверка распакованного архива и source/Docker/cleanup flags подтверждены; [пакет evidence и расследования](clean-room-b1e2ee1/RESULT.md) сохраняет исходные байты до и после исправления.

Механизм установлен направленным `42500787300`: первоначальный ведущий 1 отказал в двух из трёх попыток; между `terminated` и результатом повторных DCS-проверок стабильно проходило 10,038–10,054 секунды, затем `NO_LEADER_LEASE`, guarded return и новая попытка. `42500789794` дал PostgreSQL PASS после root/Kafka, но root обнаружил отдельный terminal failure штатно ограждённого SQL peer во время down/up. Исправлены транспорт чтения DCS и ожидание native return в пределах прежнего deadline, без изменения native safety. Локально RED/GREEN прежнего и нового кода подтвердил зависимость от первого endpoint; PostgreSQL 193 PASS, orchestration/recovery 386 PASS и один предусмотренный skip. Предварительный switchover 1/2/3 не гарантировал исходное состояние initial-1; исторический единичный PASS не заменял адресную и новую полную приёмку.

Контрольная точка: 2026-11-08 14:20 Europe/Belgrade. GitHub workflow `clean-room.yml` имеет 48 запусков, ни одного полного PASS. Текущая исходная версия до изменения диагностического подхода — `2786e210e84570c9aca4c28e28d1e0c1a347d95e`. Цель сохраняется: воспроизведение всех одиннадцати функций, проверка evidence и повторный Production Ready аудит. Выбор реализации перевода остаётся открытым; compute/provider не входит в приёмку.

## Проверенная матрица последних прогонов

| Run | Source | Последняя успешная граница | Подтверждённый отказ | Решение и состояние |
|---|---|---|---|---|
| [42431964751](https://github.com/sapogov1978/AELEV-platform-infrastructure/actions/runs/42431964751) | `96eb33d` | root start, UI, stop/up | `EXTERNAL_NETWORK_MISSING` после deep down | `8ef2b2f` восстанавливает Keycloak/network owner до потребителей; последующие root прогоны PASS |
| [42433152454](https://github.com/sapogov1978/AELEV-platform-infrastructure/actions/runs/42433152454) | `8ef2b2f` | полный root, Kafka | PostgreSQL после healthy, непрозрачная Docker-команда | Добавлена operation/reason диагностика; точная прежняя причина не установлена |
| [42455537735](https://github.com/sapogov1978/AELEV-platform-infrastructure/actions/runs/42455537735) | `f503c76` | root, Kafka, полный PostgreSQL HA/backup, MinIO | isolated Egress startup | `05515cf` исправил временный telemetry bind и проверил изоляцию |
| [42458205057](https://github.com/sapogov1978/AELEV-platform-infrastructure/actions/runs/42458205057) | `05515cf` | root, Kafka, PostgreSQL, MinIO, полный Egress | telemetry driver image отсутствовал локально | `2786e21` объявляет exact Python digest в manifest/build/preflight/BOM; сам telemetry сценарий ещё не подтверждён |
| [42461185054](https://github.com/sapogov1978/AELEV-platform-infrastructure/actions/runs/42461185054) | `2786e21` | build/preflight/BOM, root, Kafka, первоначальные SQL/TLS/quorum/provision/isolation | новый PostgreSQL writer не появился за 120 секунд после pair-loss | Непостоянный HA-отказ; не считается исправленным или инфраструктурной случайностью |
| [42485161039](https://github.com/sapogov1978/AELEV-platform-infrastructure/actions/runs/42485161039) | `1f87c20` | изолированный startup семи healthy ролей | `VERIFY_SYNCHRONOUS_QUORUM/ASSERTION_FAILED` до pair-loss; source baseline отклонил untracked failure-log | Ограниченный барьер фактической streaming quorum-реплики; owner runtime logs исключаются из source/build context. Повторная проверка требуется |
| [42485163225](https://github.com/sapogov1978/AELEV-platform-infrastructure/actions/runs/42485163225) | `1f87c20` | реальные SPIRE identity, SPIRE observation/Grafana, bounded recovery, точная cleanup всех стадий | telemetry: отсутствует Playwright в неверно выбранном owner venv; Translator: snapshot не учитывал фактически имеющийся PostgreSQL RepoDigest | Использовать уже закреплённый browser driver venv и учитывать exact digests, не скачивать/подменять образы |
| [42487453085](https://github.com/sapogov1978/AELEV-platform-infrastructure/actions/runs/42487453085) | `b7a6b4b` | три полных PostgreSQL HA/backup lifecycles с отказами 1→2, 2→3, 3→1, все cleanup/source baselines | Нет | Адресный PASS всех трёх fault targets; native safety/fencing/DCS таймеры сохранены |
| [42487455393](https://github.com/sapogov1978/AELEV-platform-infrastructure/actions/runs/42487455393) | `b7a6b4b` | полный telemetry onboarding с outage/recovery и Chromium; Translator queue/dedup/restart/Kafka outage без провайдера | Нет | Обе ранее недоступные функции получили адресный PASS |
| [42489907582](https://github.com/sapogov1978/AELEV-platform-infrastructure/actions/runs/42489907582) | `b7a6b4b` | Полная приёмка, все 11 функций, чистый source/Docker baseline, cleanup | Нет | PASS; [проверяемый пакет](clean-room-b7a6b4b/RESULT.md) независимо проверен усиленным verifier |
| [42494484872](https://github.com/sapogov1978/AELEV-platform-infrastructure/actions/runs/42494484872) | `0662a28` | root, Kafka, SQL/TLS/quorum/provision/isolation | pair-loss target 1: SQL 1/2 stopped, 3 replica, etcd 2/3 и fencer healthy; `NO_LEADER_LEASE` | Общий допуск открыт заново; audit timeline и natural-primary повторы обязательны до следующей полной приёмки |

Не выполненные в конкретном run стадии не наследуют PASS из другого commit. Предыдущие отчёты сохраняются как доказательства своих ограниченных условий.

## Цепочка и контрольные сроки

| Срок 2026-11-08 Europe/Belgrade | Результат | Условие перехода |
|---|---|---|
| 14:50 | Матрица, отдельный диагностический запуск, сохраняемая failure state | Нет правок runtime по неподтверждённой гипотезе |
| 15:50 | Изолированное воспроизведение PostgreSQL и отдельная проверка оставшихся функций | Установлен механизм отказа; после исправления PostgreSQL проходит три повтора без ослабления защиты |
| 18:20, целевой прогноз | Полная приёмка зафиксированной версии и аудит её evidence | Все 11 функций PASS, независимый verifier PASS, source/Docker baseline сохранён, CI той же версии PASS |

Сроки — рабочие контрольные точки. При риске срыва заранее фиксируются полученный результат, точный оставшийся дефект и пересмотр прогноза; ожидание или превышение срока не превращают FAIL в PASS и не останавливают работу автоматически.

## Правила исследования

1. Следующий эксперимент проверяет одну записанную гипотезу и сохраняет наблюдения, которые могут её подтвердить или опровергнуть. Возврат к отвергнутой гипотезе требует новых данных.
2. Отдельный `full-local-diagnostic` запускает выбранные настоящие owner-сценарии и максимум три повтора за invocation; результат не допускается как полное evidence. PostgreSQL и оставшиеся функции можно проверять на независимых одноразовых runners.
3. При непостоянном failover сначала исследуются fault target, состояния Patroni/etcd/fencer, native reason codes и временная последовательность. Увеличение timeout, отключение TLS/fencing/quorum и массовая замена стека не являются исправлением.
4. После сбоя сохраняются безопасные диагностические поля до cleanup. Следующий сценарий разрешён только после подтверждения исходного Docker/source baseline.
5. Полный clean-room запускается после завершения изолированных проверок. Новый полный запуск обосновывается изменением, проверенным на проблемной стадии; повтор одной общей ошибки без новых данных не считается прогрессом.

Оба старых PostgreSQL PASS отключали исходного ведущего №2. Три новых диагностических повтора должны отключать соответственно 1/2/3: будущая цель при необходимости выбирается стандартным Patroni switchover с mTLS, затем подтверждаются все три SQL-роли, единственный заданный primary и sync quorum. Случайный повтор одного удачного fault target не закрывает эту матрицу.

Гипотеза PostgreSQL о порядке обращения к остановленному DCS endpoint не получила подтверждения в трёх направленных прогонах: все три отказа прошли при прежних native safety/fencing/DCS таймерах. Точная причина исторического pair-loss timeout из run `42461185054` не установлена из-за отсутствия нужного состояния в его артефакте; устранённая ранняя readiness-гонка и новые успешные прогоны не выдаются за доказательство причины именно того старого timeout. Критерий текущей версии — адресная матрица 1/2/3 и новая полная приёмка.

К 15:25 Europe/Belgrade адресная часть контрольной точки 15:50 выполнена: три PostgreSQL fault targets, telemetry и Translator PASS; SPIRE identity, observation/Grafana и bounded recovery подтверждены предыдущей независимой партией. Полная приёмка `42489907582` запущена на том же функциональном commit. Итоговый аудит также ужесточает verifier: BOM с dirty source или чужими schema/profile/release не допускается даже после корректного пересчёта его SHA-256.

В 16:02:37 Europe/Belgrade полная приёмка завершилась PASS. Независимый усиленный verifier подтвердил 11/11 функций; пакет с исходными байтами сохранён в репозитории. Финальный аудиторский commit проходит собственные CI и полный clean-room gate перед завершением задачи.

Повторный полный run `42494484872` вновь отказал на pair-loss первоначального ведущего 1; общий допуск открыт заново. Natural-партия `42497353603` прошла 3/3 на том же runtime, но выбрала первоначальных ведущих 2/3/2. Следующий эксперимент сравнивает первоначального ведущего 1 без switchover самостоятельно и после root/Kafka. Все peer-контейнеры создаются до старта первого SQL-узла, чтобы тестовая подготовка сохраняла требования fencing. Локальный сценарий на двух CPU прошёл с первоначальным ведущим 3; отдельный ранний старт узла без созданных peers был отклонён fencing и не трактуется как исследуемый дефект. Контрольная точка диагностики перенесена на 18:00 Europe/Belgrade, целевой прогноз полной приёмки — 18:50; прежний срок 18:20 больше не считается подтверждённым прогнозом.
