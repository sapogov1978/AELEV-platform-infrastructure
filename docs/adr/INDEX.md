# Ключевые архитектурные решения Platform Infrastructure

Этот реестр содержит только действующие ключевые решения самого инфраструктурного продукта. Это не хронология изменений: даты реализации, исправления и выпуски находятся в [`CHANGELOG.md`](../../CHANGELOG.md).

## Действующие решения

| Решение | Зафиксированный выбор |
|---|---|
| [00-INFRA-20261105-0001-independent-infrastructure-product](00-INFRA-20261105-0001-independent-infrastructure-product.md) | Platform Infrastructure является самостоятельным, независимо выпускаемым продуктом; потребители не владеют его жизненным циклом или ресурсами. |
| [00-INFRA-20261106-0002-infrastructure-repository-layout](00-INFRA-20261106-0002-infrastructure-repository-layout.md) | Канонический репозиторий имеет плоский корень, а путь уже установленной зависимости и runtime-данные не переносятся вместе с исходниками. |
| [00-INFRA-20261106-0003-owner-boundaries-and-consumer-resources](00-INFRA-20261106-0003-owner-boundaries-and-consumer-resources.md) | Инфраструктура предоставляет domain-neutral capabilities; конкретные ресурсы и идентификаторы принадлежат потребителю. |
| [00-INFRA-20261106-0004-self-contained-service-delivery](00-INFRA-20261106-0004-self-contained-service-delivery.md) | Каждый сервис самостоятельно владеет поставкой, Compose, конфигурацией, данными, документацией и тестами. |
| [00-INFRA-20261106-0005-kafka-cloudevents-contracts](00-INFRA-20261106-0005-kafka-cloudevents-contracts.md) | Предметный межсервисный обмен использует Kafka, CloudEvents и владельческие версионированные schemas. |
| [00-INFRA-20261106-0006-technical-storage-boundaries](00-INFRA-20261106-0006-technical-storage-boundaries.md) | PostgreSQL, MinIO и SQLite применяются в разных owner-scoped границах без общей изменяемой базы. |
| [00-INFRA-20261106-0007-workload-and-operator-identity](00-INFRA-20261106-0007-workload-and-operator-identity.md) | Workload mTLS/SPIFFE и человеческий OIDC/PKCE разделены по identities, clients и secret roots. |
| [00-INFRA-20261106-0008-platform-ui-owner-injection](00-INFRA-20261106-0008-platform-ui-owner-injection.md) | Platform UI принимает технические owner projections и sandboxed UI, не получая предметные полномочия. |
| [00-INFRA-20261106-0009-independent-observability-and-telemetry](00-INFRA-20261106-0009-independent-observability-and-telemetry.md) | Семь observability-продуктов независимы; OTLP delivery и устойчивый журнал не смешиваются с проекциями. |
| [00-INFRA-20261106-0010-bounded-recovery-and-process-control](00-INFRA-20261106-0010-bounded-recovery-and-process-control.md) | Восстановление ограничено точной целью, историей попыток, PAUSE/leases и отдельным полномочием RESUME. |
| [00-INFRA-20261106-0011-controlled-public-egress](00-INFRA-20261106-0011-controlled-public-egress.md) | Public Egress Gateway предоставляет identity-scoped fetch без предметных решений и открытого proxy. |
| [00-INFRA-20261106-0012-translator-technical-capability](00-INFRA-20261106-0012-translator-technical-capability.md) | Translator владеет очередью и Kafka-контрактом, но использует непрозрачный провайдер и ничего не знает о модели или способе перевода. |
| [00-INFRA-20261106-0013-owner-certificate-lifecycle](00-INFRA-20261106-0013-owner-certificate-lifecycle.md) | Сертификаты, private keys, approval и применение ротации остаются у владельца leaf. |
| [00-INFRA-20261106-0014-product-readiness-and-site-admission](00-INFRA-20261106-0014-product-readiness-and-site-admission.md) | Готовность продукта отделена от допуска конкретной площадки и полномочий на runtime-воздействие. |
| [00-INFRA-20261106-0015-clean-room-full-local-reproduction](00-INFRA-20261106-0015-clean-room-full-local-reproduction.md) | Полный локальный профиль фиксируется manifest, preflight, release BOM и доказательством всех обязательных функций из чистого состояния. |
| [00-INFRA-20261109-0016-opaque-provider-runtime-egress](00-INFRA-20261109-0016-opaque-provider-runtime-egress.md) | Выбранный immutable opaque provider входит в root/full-local с исходящей сетью и generic secret mount без раскрытия реализации Translator. |

## Правило нового ADR

Собственный ADR создаётся только для нового или изменённого межвладельческого архитектурного выбора и называется `00-INFRA-YYYYMMDD-NNNN-essence.md`. Он обязан содержать контекст, список ключевых решений, последствия, проверку/откат и официальные основания. Операционная история, результаты тестов и перечень выпусков в ADR не добавляются.

## Унаследованные решения

Сто тридцать два унаследованных ADR сохранены побайтно в [архиве выделения](../audit/inherited-adrs/INDEX.md), потому что их исходные identity, статусы, supersedence и SHA-256 являются provenance. Они не образуют список текущих решений Platform Infrastructure. Машиночитаемые identity и hashes находятся в [`docs/audit/extraction.json`](../audit/extraction.json); связь действующих решений с официальными первичными источниками ведётся в самих `00-INFRA-*` и [реестре оснований](../references/official-sources.md).
