# 00-INFRA-20261106-0004-self-contained-service-delivery — Самодостаточная поставка и композиция сервисов

- **Статус:** Принят
- **Дата:** 2026-11-06
- **Происхождение:** неизменяемые ADR-0082, ADR-0088, ADR-0089, ADR-0090, ADR-0094, ADR-0104, ADR-0113 и ADR-0134 из [архива выделения](../audit/inherited-adrs/INDEX.md)

## Контекст

Общая установка должна компоновать самостоятельные сервисы, не копируя их конфигурацию и не создавая второго владельца Compose-рецептов.

## Ключевые решения

- Каждый сервис самостоятельно содержит Compose, Dockerfiles, конфигурацию, secrets templates, README, SPEC, USER_MANUAL, uppercase `INDEX.md` как агентскую карту «что и где искать», диаграмму и тесты.
- Корневая композиция включает владельческие рецепты без переименования ролей, исправления env и повторного объявления runtime.
- Локальные сети, volumes и административные задачи принадлежат тому сервису, который их создаёт.
- Совместно размещённый потребитель подключается к объявленной ingress-сети провайдера только своим владельческим overlay; удалённый режим использует маршрутизируемый HTTPS endpoint. Общая сеть не заменяет TLS, SAN или workload identity и не открывает backend-хранилище.
- Когда существующие самостоятельные потребители используют локальный `host-gateway`, провайдер может вторично опубликовать тот же TLS endpoint только на private IPv4 gateway стандартного Docker bridge. Такой listener не заменяет основной адрес, не использует wildcard/LAN bind и не ослабляет CA/SAN.
- Общий build только собирает выбранные образы и никогда не запускает контейнеры.
- Общие и самостоятельные режимы не могут одновременно писать в одни данные.
- Публичный план установки стабилен; изменение capability, владельца или межсервисного контракта требует отдельного ADR.
- HTTP-пробы используют единые `/healthz` и `/readyz`, но готовность остаётся владельческим условием сервиса.

## Последствия

- Сервис можно собрать, проверить, развернуть и восстановить независимо.
- Корневой Compose остаётся технической композицией, а не общим runtime-владельцем.
- Consumer shutdown не получает неявного права на infrastructure shutdown.

## Проверка и откат

CI inventory обнаруживает каждый Python/UI-проект, Compose проходит render, build-plan покрывает все локальные образы, а boundary-gate запрещает sibling source imports. Откат выбирает прежний совместимый выпуск сервиса без удаления его volumes и identities.

## Официальные основания

- [Docker Compose include](https://docs.docker.com/reference/compose-file/include/) определяет включаемые самостоятельные application models.
- [Compose services](https://docs.docker.com/reference/compose-file/services/), [networks](https://docs.docker.com/reference/compose-file/networks/) и [volumes](https://docs.docker.com/reference/compose-file/volumes/) определяют владельческие runtime-ресурсы.
- [Compose ports long syntax](https://docs.docker.com/reference/compose-file/services/#ports) определяет явный host IP для каждого опубликованного listener.
- [Compose secrets](https://docs.docker.com/reference/compose-file/secrets/) отделяет секреты от образов и обычной конфигурации.
