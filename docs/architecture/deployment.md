# Развёртывание Platform Infrastructure

Полный штатный состав запускается `bootstrap.py --full-runtime` по [решению 0017](../adr/00-INFRA-20261112-0017-full-runtime-startup.md). Его [последовательность](full-runtime-startup.mmd) использует канонический manifest и владельческие Compose, сохраняет предоставленные credentials и инкарнации и требует handoff при drift. Техническое здоровье состава не подменяет quality admission и функциональную приёмку.

Нормативная граница определяется [самостоятельным продуктом](../adr/00-INFRA-20261105-0001-independent-infrastructure-product.md), [границами владельцев](../adr/00-INFRA-20261106-0003-owner-boundaries-and-consumer-resources.md) и [самодостаточными поставками](../adr/00-INFRA-20261106-0004-self-contained-service-delivery.md). Этот документ принадлежит инфраструктуре и не требует архитектуры конкретного потребителя.

## Композиция

| Возможность | Владелец и источник конфигурации | Публичная техническая граница |
|---|---|---|
| Межсервисный транспорт | [Kafka](../../kafka/SPEC.md), собственный Compose | Kafka; ресурсы прикладного клиента создаёт клиент |
| Транзакционное хранение | [PostgreSQL](../../postgresql/SPEC.md), три SQL-узла, etcd и явно выбранное fencing | TLS PostgreSQL; отдельная административная подготовка базы/роли |
| Объектное хранение | [MinIO](../../minio/SPEC.md), собственные sites и volumes | S3; buckets и credentials потребителя принадлежат ему |
| Операторский вход и workload identity | [Keycloak](../../keycloak/SPEC.md), [SPIRE](../../spire/SPEC.md) | OIDC/PKCE и SPIFFE; административные полномочия не выдаются runtime-клиентам |
| Навигация и состояния | [Platform UI](../../platform-ui/SPEC.md) | Владельческие декларации, состояния и изолированный browser UI |
| Наблюдаемость | [Семь самостоятельных продуктов](../../observability/SPEC.md) | Метрики, журналы, трассы и устойчивая техническая доставка |
| Публичный сетевой доступ | [Public Egress Gateway](../../public-egress-gateway/SPEC.md) | Ограниченный fetch с точным разрешением и mTLS |
| Перевод | [Translator](../../translator-service/SPEC.md) | Версионированные Kafka-контракты и непрозрачный порт исполнения |

[Общая диаграмма](../../infrastructure.mmd), [композиция наблюдаемости](../../observability/observability.mmd) и [границы репозиториев](repositories.mmd) дополняют таблицу. Детальная диаграмма каждого владельца находится в его `architecture.mmd`; параметры, отказные инварианты и команды — в собственных SPEC и USER_MANUAL.

## Профили и приёмка

Core CLI по [контракту установки](../api/installation.md) готовит локальный состав, включая capability `translation`. Полный `full-local` задаёт [manifest](../../full-local.json): 15 владельцев, workload identity, наблюдаемость и разные подписанные provider/worker image digests. Реализация провайдера остаётся в отдельной поставке и видна композиции только через image-owned healthcheck, generic secret mount и opaque UDS; при её недоступности Translator сохраняет принятую работу.

`init` сохраняет существующие env/identities, `build` не запускает процессы. [Clean-room runbook](../operations/clean-room-reproduction.md) задаёт входы, build, preflight, BOM, реальные lifecycle-проверки и независимый verifier. [Аудит готовности](../audit/production-readiness.md) связывает вывод с точными source commits и evidence; один health endpoint или старый отчёт не допускает новый выпуск.

## Граница конфигурации инфраструктуры

Корень включает канонические владельческие рецепты, не копирует runtime-определения и не становится каталогом приложений. Прикладные topics, databases, buckets, OIDC clients, dashboards и grants создаёт потребитель через публичный административный вход соответствующего владельца. Общая и самостоятельная композиции не могут одновременно писать в одни volumes. Consumer shutdown не останавливает инфраструктуру.

Сеть провайдера не заменяет TLS, SAN или identity. Локальный bridge не является межсерверной сетью. Production-площадка отдельно проверяет маршрутизацию, DNS/firewall, PKI/KMS/SVID, capacity, независимые failure domains, backup target, RPO/RTO и доставку тревог. Ограниченное восстановление сохраняет журнал и бюджет по [операторскому руководству](../../USER_MANUAL.md); запуск или обновление выпуска не разрешает возобновление приостановленной обработки.

## Официальные основания

- [Docker Compose include](https://docs.docker.com/reference/compose-file/include/) — композиция самостоятельных application models.
- [Compose profiles](https://docs.docker.com/compose/how-tos/profiles/) — явное включение профилей.
- [Закрепление образов digest](https://docs.docker.com/dhi/core-concepts/digests/) — неизменяемые входы выпуска.
