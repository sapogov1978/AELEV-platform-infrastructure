# 00-INFRA-20261116-0019-pinned-owner-compose-execution — Выполнение выбранного владельческого рецепта

- **Статус:** Принят как реализация подтверждённой независимости ролей 00-INFRA-0018
- **Дата:** 2026-11-16
- **Уточняет:** 00-INFRA-0017, 00-INFRA-0018; стек, владельцы и межсервисные контракты сохраняются

## Контекст

Узловой исполнитель фиксирует отдельные публичные процедуры SPIRE и Translator. Для произвольной роли аутентифицированного владельческого каталога требуется общий исполнитель без списка приложений внутри инфраструктуры. Compose `up` без ограничений может поднять соседние зависимости или изменить существующие контейнеры.

## Ключевые решения

- Процедура `owner-compose` принимает явно подготовленные source manifest, каталог, роль, recipe subset, закрытую конфигурацию и локальное рабочее окружение. Прикладные роли, inputs, порядок и admission задаёт владелец или вызывающий установщик.
- Закреплённый инфраструктурный executor source и source владельца имеют отдельные roots/manifests. Каталог и recipes выбираются только из owner release, код helpers — только из executor release. Копирование инфраструктурных tools в репозиторий потребителя не требуется; на целевой узел доставляются независимые immutable generations.
- Выбранный source/helper/recipe и private inputs проверяются по SHA. Разобранный Compose model сохраняется как закрытый временный snapshot; все последующие config/hash/start используют его и пустой explicit env file. Operator endpoints, command/entrypoint и opaque provider payload не переписываются.
- Runtime создаётся только для выбранной роли с `--no-deps --no-build --pull never --no-recreate`. Registry digest или native image ID должен существовать на целевом daemon. Build/pull не смешиваются со start.
- Существующий экземпляр принимается только с точными project/service/config hash/native image. Остановленная роль, конфигурационный drift, лишняя replica или legacy native restart требуют владельческого handoff. Общий исполнитель не выполняет recovery, stop/down, replacement или RESUME.
- Operation использует канонический service command через одноразовый `run --rm --no-deps --pull never` без command override. Его outcome/journal сохраняет node executor; неизвестный результат не повторяется.
- Возможность запуска выбранной роли не подтверждает её текущую readiness, dependency/quality admission или полный inventory workflow. Межрольные `service:` namespaces, links и volumes_from не становятся обязательной средой независимой роли.

## Последствия

Новые владельцы используют общий технический capability без импорта их предметной семантики в инфраструктуру. Source и private workspace разделены; подготовка разрешённых входов, dependency barriers, credentials/identity delivery и общий DAG остаются обязанностью owner workflow.

## Проверка и откат

Проверяются настоящий child-process/container effect, независимость соседней роли, повтор без пересоздания, input/source/config/image drift, stopped-role refusal и сохранность собственного состояния. Execute/status используют прежний fsync intent/receipt, local/SSH policy и reconciliation. Возврат к прежнему helper не переписывает незавершённые операции; текущий source и журналы сохраняются.

## Официальные основания

- [Compose config](https://docs.docker.com/reference/cli/docker/compose/config/) определяет resolved model и service configuration hashes.
- [Compose up](https://docs.docker.com/reference/cli/docker/compose/up/) определяет no-deps/no-build/pull/no-recreate; конкретную политику handoff задаёт продукт.
- [Compose run](https://docs.docker.com/reference/cli/docker/compose/run/) определяет отдельную задачу, no-deps и rm; исходный command сохраняется без дополнительных аргументов после service.
- [Compose startup order](https://docs.docker.com/compose/how-tos/startup-order/) отделяет запущенный процесс от готовности зависимости.
