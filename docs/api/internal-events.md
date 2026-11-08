# Владение Kafka-событиями инфраструктуры

Общая граница определяется [00-INFRA-20261106-0005-kafka-cloudevents-contracts](../adr/00-INFRA-20261106-0005-kafka-cloudevents-contracts.md). Kafka предоставляет транспорт; издатель владеет topic, schema, partition key, версией и provenance. Каталоги предметных событий и схемы конкретных приложений остаются у потребителей.

## Envelope и устойчивость

Межсервисный обмен использует версионированный CloudEvents envelope. Точный набор обязательных полей, ограничения payload и security rules определяет закреплённый контракт владельца, а не второй общий перечень в этой статье. Повтор технической доставки сохраняет исходные bytes и identity; тот же идентификатор с другим содержимым не считается безопасным повтором.

Consumer проверяет schema и полномочия, обеспечивает идемпотентность и подтверждает offset только после своей устойчивой транзакции. Broker ACK не доказывает предметное применение получателем. Между разными topics не предполагается общий порядок; несовместимое изменение схемы, ключа или смысла требует отдельной версии владельческого контракта. История topic и схем не переписывается при откате.

## Контракты, принадлежащие этому продукту

| Событие | Topic | Владелец контракта и издатель | Partition key |
|---|---|---|---|
| `TranslationRequestedV2` | `translation.requests.v2`, `translation.requests.priority.v2` | Translator; публикует авторизованный технический клиент | `request_id` |
| `TranslationCompletedV2` | `translation.results.v2` | Translator; публикует translator-service | `request_id` |
| Миграционные `TranslationRequested` / `TranslationCompleted` v1 | `translation.requests.v1`, `translation.requests.priority.v1`, `translation.results.v1` | Translator; только явно поддерживаемая совместимость | `request_id` |

Действующий [Kafka-контракт перевода](translation.md) задаёт поля, limits, dedup и сопоставление результата. Исполняемый источник проверки — [поставляемый schema artifact](../../translator-service/contracts/translation-v1.json), его [SHA-256](../../translator-service/contracts/translation-v1.sha256) и [owner validator](../../translator-service/src/translator_service/contracts.py). Историческое имя файла не ограничивает набор содержащихся в нём v1/v2 schemas. Пример клиента не создаёт инфраструктурного каталога приложений.

Телеметрия и UI presence используют собственные [технические протоколы](telemetry.md) и [контракт инъекции](platform-ui-injection.md), а не выдуманные общие Kafka-семейства. Они не заменяют предметную Kafka-границу. Приватные ключи, access tokens и credentials в события не включаются; разрешённое содержание payload задаёт конкретный контракт.

## Канонические источники и история

Нормативная цепочка: ADR → владельческий API → закреплённый service-local artifact/digest → validator и contract tests. Новый контракт публикуется владельцем; сгенерированный artifact не исправляется как вторая независимая схема. Прежний каталог чужих событий сохранён побайтно в [аудиторском архиве](../audit/pre-1.0-api-documents.tar.gz) с [описью SHA-256](../audit/pre-1.0-api-documents.json) и не является API инфраструктуры.

## Официальные источники

- [CloudEvents 1.0.2](https://github.com/cloudevents/spec/blob/v1.0.2/cloudevents/spec.md).
- [Apache Kafka 3.9](https://kafka.apache.org/39/documentation/).
- [JSON Schema 2020-12](https://json-schema.org/draft/2020-12).
