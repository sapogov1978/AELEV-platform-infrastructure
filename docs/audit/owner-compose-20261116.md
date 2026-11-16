# Локальная квалификация owner-compose — 16 ноября 2026

Проверяется отдельный infrastructure capability, а не готовность всей платформы. [Машиночитаемый отчёт](owner-compose-20261116.json) связывает helper SHA, сценарии и receipts. Ранее опубликованный stable остаётся 1.1.7; source 1.1.8 — локальный кандидат до обязательных remote gates. По явному ответу владельца «Пока продолжай только локально» новые cloud jobs, push и выпуск не выполняются.

## Проверенный scope

| Связка | Проверка и результат |
|---|---|
| Executor source и owner source | Разные roots/manifests, helpers отсутствуют в owner source; настоящий node executor исполнил owner procedure из этих roots |
| Опубликованный DRP owner catalog | Два настоящих Advance/Deep role bindings и 1316 source files прошли source/recipe проверку. Private configuration/model не рендерились и readiness не заявлена |
| Linux controller | Существующий image `2a668595…146ecad3`, Python 3.13.7, Compose 2.40.3; private filesystem отдельно от host source, code доставлен с SHA-256 |
| Runtime / replica | Два настоящих экземпляра на одном Docker Engine, разные IDs/volumes; соседняя роль не запущена; повтор сохранил исходный контейнер и единственный start effect |
| Operations | Канонические success/failure tasks исполнились ровно один раз после replay; native failure exit 7 сохранён |
| Handoff | Изменённый configuration hash отвергнут; stopped instance не возобновлён, его история сохранена |
| Cleanup | Свои roles/volumes/networks и Linux controller удалены по проверенным labels; baseline 177 non-ephemeral owner containers сохранён |

Контроллер использует собственную Linux filesystem и явный operator Docker socket одного Engine. Это проверка target-side исполнения, а не SSH transport, два daemon или разные физические компьютеры. Private evidence хранится вне checkout; публикации credentials/runtime data нет. Physical proof отложен в [TODO-MVP](../../TODO-MVP.md), остальные readiness/E2E gates этим отчётом не закрыты.

## Найденные расхождения

Первая схема требовала один source root для helpers и владельческого рецепта. При проверке связи с отдельным consumer repository обнаружена невозможность исполнить его без смешения исходников. До выпуска введены независимые `executor_root/executor_files` и `owner_root/owner_files`; прежние single-source receipts сохранены отдельно. Scope перепроверен на настоящем DRP archive и native двухгенерационном сценарии. Прогноз исправления перед правкой — 20–30 минут.

Локальный Compose 5.3.0 принимал `--no-tty`, тогда как официальный [2.24.4 run.go](https://github.com/docker/compose/blob/v2.24.4/cmd/compose/run.go) и [2.40.3 run.go](https://github.com/docker/compose/blob/v2.40.3/cmd/compose/run.go) объявляют `--no-TTY`/`-T`. Existing Linux operator image воспроизвёл `unknown flag: --no-tty`; тот же probe с `-T` прошёл, затем прошли все 10 execution cases на Linux 2.40.3. Минимальный 2.24.4 проверен по официальному интерфейсу, отдельный runtime этого номера не запускался. У прежнего CI `ecbb701` было 25/26: отказ на task; его полный private stderr не был загружен, исторический failure не заменён локальным PASS.

Первоначальный Docker harness столкнулся с исчерпанным default address pool: исправлен только scoped test IPAM, по [Compose networks](https://docs.docker.com/reference/compose-file/networks/#ipam), без изменения daemon/рабочих сетей. Последующие baseline/readiness/newline ошибки самого harness также сохранены отдельно. Для Linux controller `docker cp` в tmpfs заменён фиксированным `docker exec` stream: это соответствует документированным [Docker cp corner cases](https://docs.docker.com/reference/cli/docker/container/cp/#corner-cases), source hashes проверены, архив private результатов проверен перед извлечением. Runtime алгоритм ради этих ошибок harness не переписывался.

## Результат и следующий gate

После финального согласования timeout semantics прошли 36 node/owner regressions и 204 root tools tests, Black/Ruff, strict mypy трёх новых/затронутых executable modules. Timeout mutating команды сохраняется как `RESULT_UNKNOWN`; тот же operation не исполняется повторно. Повтор полного контейнерного scope выполнен после изменения этого handler на точном новом коде. Текущий receipt Linux controller — `bf45e6a9b86987140ddd537c7bd062d407ebaf26aed0eea54ffbdbf64f4d7035`, inner qualification — `4eef5fb34bba3bd30e542941c6d0e30c8b4c29db1941598935b20ee59f4c8e69`. Прежний двухгенерационный receipt `169215af…295f1e0` сохранён отдельно.

CI DRP `d9ebaf9` не стартовал пять initial jobs из-за GitHub billing/spending limit: steps пусты, annotation проверена. Это внешний отказ запуска, не failed source assertion. Старый full gate `ecbb701` отменён как superseded до выпуска. После восстановления Actions понадобятся audit и новый exact-source clean-room локального кандидата; они не заменяются этим component proof. Source release, общий inventory execution, настоящий policy/queue E2E и итоговый production-ready статус остаются отдельными результатами.

Последующий локальный [SDK bridge](owner-workflow-bridge-20261116.md) имеет отдельные hashes/receipts: safe result projection, metadata больше 64 KiB, 207 tools tests и повторные native checks. Приведённые выше 204 tests и `bf45e6…` относятся к предыдущему executor snapshot, а не к новому bridge. Исторические доказательства сохраняются отдельно.
