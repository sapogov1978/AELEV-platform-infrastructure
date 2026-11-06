# ADR-0128: Самостоятельная оболочка и неактивный запуск Base Screening

> **Происхождение standalone-редакции.** Источник: `docs/adr/20261014-0128-base-screening-service-envelope.md`; SHA-256: `0fdadb104aa946678112d16db39396408f5eeb009581de683d4e0bc7e8df1868`; основание включения: прямая ссылка из поставки. Сохранены номер, статус, история замены и применимые решения; упоминания исключённых bounded contexts нейтрализованы без переноса их контрактов.

- **Статус:** Частично заменён [ADR-0132](20261015-0132-base-screening-inline-queue-processing-control.md) в составе обычного Compose и способе включения `triage-worker`, [ADR-0135](20261015-0135-base-screening-publication-independent-processing.md) в постоянной публикации outbox и [ADR-0139](20261016-0139-base-screening-product-readiness-boundary.md) в границе между готовностью продукта и допуском конкретной production-платформы; остальные решения остаются принятыми
- **Дата:** 2026-10-14
- **Основание:** A5, A10, A12, ADR-0082, ADR-0099, ADR-0101, ADR-0107, ADR-0108, ADR-0111, ADR-0119
- **Частично заменяет (Supersedes):** [ADR-0079](20261005-0079-base-screening-service-name.md) в префиксе окружения и поддержке переходных псевдонимов; имя владельца, сохранённые wire-идентификаторы, данные и offsets не меняются

## Контекст

Base Screening должен подниматься как самостоятельный функциональный продукт по тем же эксплуатационным принципам, что Policy Management и Zone Ingestion, но первый запуск не должен начать потребление доменных очередей или Web-обход. Прежняя поставка зависела от контекста корня монорепозитория, использовала несколько поколений env-имён и не включала полный owner-controlled контур Platform UI, SSO и устойчивой телеметрии.

## Решение

Единственный префикс настроек владельца — `AELEV_BSS_*`. `AELEV_BASE_SCREENING_*`, `AELEV_CRAWLER_*` и общие прикладные `AELEV_*` больше не являются runtime-псевдонимами; сохранённые имена Kafka topics, consumer group, CloudEvents source, таблиц, policy ID и модельных артефактов остаются совместимыми идентификаторами того же владельца и не переписываются.

Канонический Compose без профилей запускает только одноразовый `migrator`, `api`, самостоятельный `ui` и owner-local `telemetry-sender`. Эти роли не читают и не публикуют доменные Kafka-сообщения и не выполняют Web-обход. Все постоянные потребители, издатели и рабочие циклы включаются только явным профилем `active`. Профиль `ui` добавляет только издателя технического описания и состояния Platform UI и не активирует предметную очередь.

Backend принимает только JWT точной аудитории `base-screening` с ролью `discovery.web.operator`. UI и самодокументируемый Swagger имеют отдельные публичные OIDC-клиенты, используют Authorization Code с PKCE S256 и общую SSO-сессию `platform-operator`, не передают token Platform UI и не сохраняют access token в browser storage. Owner-controlled publisher регистрирует функциональный модуль через технический API Platform UI.

Поставка backend и UI собирается только из дерева `services/base-screening`: общие технические библиотеки и UI-kit входят как закреплённые проверяемые артефакты, а сгенерированный контрактный bundle имеет manifest с SHA-256. Телеметрия передаётся по HTTPS/mTLS в co-located устойчивый sender, правила и dashboard принадлежат сервису, а публикация dashboard выполняется отдельной одноразовой задачей. OIDC, Kafka, PostgreSQL, gateway и Grafana credentials поступают только через принадлежащие поставке файлы секретов.

## Последствия

Оператор может безопасно проверить оболочку, readiness, UI, авторизацию и наблюдаемость до разрешения обработки накопленного frontier. Включение `active` является отдельным явным действием и не следует из успешного старта API. Цена решения — одноразовая миграция env и обязательная подготовка точных OIDC-клиентов, файлов identity и каталогов секретов; молчаливый fallback к прежним именам запрещён.

Самостоятельность сборки закрывает зависимость runtime-образа от соседних исходников, но не объявляет готовыми production PKI, Kafka ACL, backup/restore, supply-chain gates, реальную межстрановую сеть или производительность полного backlog. Реестр готовности остаётся `IN_PROGRESS`, пока эти ворота не проверены отдельно.

## Проверка

1. `docker compose config` без профилей возвращает ровно `migrator`, `api`, `ui` и `telemetry-sender`; среди них нет Kafka consumer, outbox publisher или Web worker.
2. Профиль `active` добавляет полный согласованный набор рабочих ролей без изменения данных, consumer group или topic names.
3. Runtime отвергает старые env-псевдонимы, чужую audience и token без `discovery.web.operator`.
4. Backend и UI собираются с контекстом собственного каталога, а digest-проверка vendored libraries и contract bundle проходит до запуска.
5. Swagger выполняет собственный PKCE-flow, publisher доставляет описание и состояние, телеметрия не теряется при временной недоступности внешнего приёмника.

## Миграция и откат

Перед первым запуском оператор переносит значения из прежних переменных в точные `AELEV_BSS_*`, создаёт secret-файлы и проверяет resolved Compose без вывода значений. Затем выполняются provisioning jobs и неактивный запуск; backlog и offsets не меняются. Профиль `active` включается только после отдельной проверки readiness, Kafka ACL, policy projection и gateway grant.

Откат останавливает новые роли без удаления томов, secret-файлов, БД или offsets и возвращает совместимый прежний образ с прежним env-контрактом. Одновременный запуск двух поставок на одной БД или consumer group запрещён.

## Связанные документы

- [Развёртывание](https://github.com/sapogov1978/discovery-research-platform/blob/8d1a663319ac15d8729cf1422858f37d57096c75/docs/architecture/21-deployment.md)
- [Platform UI](https://github.com/sapogov1978/discovery-research-platform/blob/8d1a663319ac15d8729cf1422858f37d57096c75/docs/architecture/22-platform-ui.md)
- [Административный API](https://github.com/sapogov1978/discovery-research-platform/blob/8d1a663319ac15d8729cf1422858f37d57096c75/docs/api/base-screening-admin.md)
- [Нормативная спецификация](https://github.com/sapogov1978/discovery-research-platform/blob/8d1a663319ac15d8729cf1422858f37d57096c75/services/discovery-engine/base-screening/SPEC.md)
