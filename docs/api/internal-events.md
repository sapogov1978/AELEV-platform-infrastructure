# Внутренние события

Этот документ задаёт общий нормативный конверт, каталог семейств и правила совместимости Kafka-событий Discovery Research Platform. Поля конкретного `data` определяются владельцем в service-local schema artifact; документация не создаёт второго определения схемы.

## Конверт CloudEvents

Каждое межсервисное предметное сообщение является CloudEvents 1.0 в structured JSON и содержит `specversion`, неизменяемые `id`, `source`, `type`, `time`, `subject`, `datacontenttype`, `dataschema`, `correlationid`, `causationid`, `traceparent` и `data`. `data` содержит `schema_version`, `recorded_at` и `provenance` с точным владельцем, издателем, версией издателя и входными ссылками.

`time` равен неизменяемому `data.recorded_at`. Повторная отправка и replay сохраняют исходные байты, `id`, `time` и `recorded_at`; исправление или новая редакция получает новый идентификатор и явно ссылается на прежнюю запись.

Идемпотентность проверяется по `source + id` и предметному ключу либо поколению. Точный повтор безопасно игнорируется после подтверждения локального результата; другие байты с тем же `source + id` закрыто отклоняются и попадают в owner-local аудит.

Потребитель использует `read_committed`, проверяет schema, provenance, допустимого издателя, размер и поколение до предметной обработки и фиксирует offset только после устойчивой локальной транзакции. Между разными topics порядок не предполагается; отзыв или tombstone не позволяет позднему событию вновь активировать отменённую работу.

## Каталог активных и совместимых семейств

| Семейство | Владелец-издатель → потребитель | Topic | Ключ раздела | Исполняемый источник |
|---|---|---|---|---|
| `DomainDelegationSnapshot` | Zone Ingestion → Base Screening | `discovery.domain-delegation-snapshots.v1` | `zone_name` | `zone-ingestion/contracts` |
| `DomainDelegationDeltaBatch` | Zone Ingestion → Base Screening | `discovery.domain-delegation-deltas.v1` | `zone_name` | `zone-ingestion/contracts` |
| `DomainDelegationLifecycleChanged` | Zone Ingestion → подписанные стадии | `discovery.domain-delegation-lifecycle.v1` | `domain_name` | `zone-ingestion/contracts` |
| `TemporaryZoneFileSubmitted` | Deep Screening → Zone Ingestion | `discovery.temporary-zone-files.v1` | `temporary_zone_file_id` | `zone-ingestion/contracts` |
| `DiscoveryStageCommand` | Base Screening → Advance Screening | `discovery.stage-commands.v1` | `frontier_id` | `base-screening/contracts` |
| `AdvanceScreeningSiteAccepted` | Advance Screening → Deep Screening | `discovery.advance-screening.site-accepted.v1` | `site_id` | `advance-screening/contracts` |
| `AdvanceScreeningSiteAcceptanceRevoked` | Advance Screening → Deep Screening | `discovery.advance-screening.site-acceptance-revoked.v1` | `site_id` | `advance-screening/contracts` |
| `DiscoveryScopeModeChangedV2` | Discovery Scope → Deep Screening | `discovery.scope-mode.v2` | `scope_id` | `discovery-scope/contracts.py` |
| `UserResearchTargetAdmittedV2` | Discovery Scope → Deep Screening | `discovery.user-research-target-admissions.v2` | `target_id` | `discovery-scope/contracts.py` |
| `UserResearchTargetRevokedV2` | Discovery Scope → Deep Screening | `discovery.user-research-target-revocations.v2` | `target_id` | `discovery-scope/contracts.py` |
| `SensorSurfacePackagePublishedV4` | Deep Screening → внешний потребитель | `discovery.sensor-surface-packages.v4` | `target_id` | `deep-screening/contracts.py` |
| `SensorSurfacePackageRevokedV4` | Deep Screening → внешний потребитель | `discovery.sensor-surface-package-revocations.v4` | `target_id` | `deep-screening/contracts.py` |
| `PolicyPublishedV4` | Policy Management → функциональные потребители | `policy.published.v4` | `policy_ref` | `policy-management/contracts` |
| `PolicyLocalizationRequestedV4` | функциональный потребитель → Policy Management | `policy.localization-requests.v4` | `request_id` | `policy-management/contracts` |
| `PolicyLocalizationProvidedV4` | Policy Management → функциональные потребители | `policy.localization-results.v4` | `request_id` | `policy-management/contracts` |
| `TranslationRequestedV2` | владелец текста → Translator | `translation.requests.v2` | `request_id` | контракт перевода и service-local schemas |
| `TranslationRequestedPriorityV2` | владелец текста → Translator | `translation.requests.priority.v2` | `request_id` | контракт перевода и service-local schemas |
| `TranslationCompletedV2` | Translator → владелец текста | `translation.results.v2` | `request_id` | контракт перевода и service-local schemas |
| `DiscoveryDeliveryRecorded` | отправитель → получатель | owner-declared `*.delivery-recorded.v1` | `delivery_id` | service-local AsyncAPI |
| `DiscoveryDeliveryAcknowledged` | получатель → отправитель | owner-declared `*.delivery-acknowledged.v1` | `delivery_id` | service-local AsyncAPI |
| `DiscoveryDeliveryReplayRequested` | получатель → отправитель | owner-declared `*.delivery-replay-requests.v1` | `delivery_id` | service-local AsyncAPI |
| `RuntimeHealthEvent` | владелец runtime → его технический контур | owner-declared health topic | `component_id` | service-local AsyncAPI |

V1–V3 пакетов Deep Screening и V1–V3 policy/translation families остаются только совместимыми историческими входами там, где это прямо реализовано consumer. Новый издатель использует текущую major-версию; сохранённое событие старой версии не переписывается под новую schema.

`DiscoveryClassifierReplica`, `DiscoveryClassifierReplicaChunk` и `DiscoveryStageResult` сохраняются в service-local bundles только для чтения существующей истории и совместимости стадий. Base Screening не публикует model output и не получает право координировать последующие решения.

## Граница Discovery

Zone Ingestion является единственным издателем рабочего domain frontier. `TemporaryZoneFileSubmitted` не создаёт работу Base Screening напрямую: Zone Ingestion проверяет неизменяемый файл, создаёт свою ревизию и публикует обычный delegation event.

`DiscoveryStageCommand` является единственным активным предметным выходом Base Screening. Он содержит точную страницу, сохранённый ограниченный материал, `policy_ref`, буквальные сигналы, происхождение адреса и `delegation_guard`, но не выбирает реализацию или модель следующей стадии.

`AdvanceScreeningSiteAccepted` нейтрален к внешнему использованию и не назначает Deep Screening владельцем чужого состояния. Отзыв содержит точную `acceptance_id`, причину и версию, которую запрещено оживлять поздним повтором.

`target_id` обозначает точный нормализованный URL, включая путь; `origin_id` обозначает только техническую границу origin. Допуск одного target не разрешает соседний путь. Deep Screening связывает пакет с точными `target_id`, `site_id`, `policy_ref`, входными событиями, снимками и digest результата.

## Policy и перевод

`PolicyPublishedV4` переносит полный неизменяемый снимок с `policy_ref`; внутренняя числовая редакция Policy Management не становится межсервисным идентификатором. `PolicyLocalizationRequestedV4` содержит только язык и идентичность запроса; Policy Management сам закрепляет текущий `policy_ref` и создаёт полный набор строк попытки.

Translator получает точные текст, исходный и целевой языки, digest и идентификатор запроса без знания политики, стадии или назначения результата. Он передаёт текст и языки непрозрачному провайдеру, не выбирает модель или движок и не хранит provider-specific сведения. Частичный набор строк не публикуется как готовая локализация.

## Устойчивая доставка

Владелец сохраняет exact payload, SHA-256, topic, partition key, generation и состояние попытки до публикации. ACK Kafka доказывает приём broker, но не подтверждает устойчивое применение получателем; для границ, требующих такого доказательства, используются `DiscoveryDeliveryRecorded` и `DiscoveryDeliveryAcknowledged`.

Replay повторяет исходные байты и не создаёт новую предметную версию. Получатель хранит tombstone и sequence boundaries; пересоздание текущей декларации отправителя не восстанавливает утраченную историю получателя.

## Совместимость

Добавление необязательного поля допустимо только при сохранении прежнего смысла и доказанной совместимости потребителей. Изменение required field, смысла поля, topic, partition key, owner, security rule или порядка применения создаёт новую major-версию схемы и topic. Consumer новой версии развёртывается раньше publisher; версии работают параллельно до подтверждённого drain.

Исторический идентификатор `DISCOVERY_REGISTRY` в ранее выпущенных delivery schemas остаётся только идентификатором совместимости. Он не обозначает общий сервис, registry или владельца данных и не применяется в новых семействах.

## Идентичность и безопасность

Production publisher аутентифицируется в Kafka точной короткоживущей SPIFFE/SPIRE identity. ACL по умолчанию запрещают доступ и разрешают конкретной роли только её topics, consumer groups и операции. Идентичность одной среды недействительна в другой.

Секреты, access token, закрытые ключи, полный текст документов, произвольный операторский ввод и необязательные provider details не входят в конверт, логи или метрики. Ошибка schema, provenance, identity, generation либо размера закрыто отклоняет событие и не подтверждает offset.

## Канонические исполняемые артефакты

Нормативная цепочка для каждого семейства имеет вид: этот каталог → owning API contract → service-local `contracts/manifest.json` и `contracts/asyncapi.json` либо экспортированный schema bundle → consumer tests. [Таблица трассируемости](https://github.com/sapogov1978/discovery-research-platform/blob/8d1a663319ac15d8729cf1422858f37d57096c75/docs/audit/TRACEABILITY.md) закрепляет конкретные пути и проверки.

Существующий service-local artifact изменяется только через его владеющий generator или модель. Сгенерированный JSON не исправляется как канонический источник. Отсутствие export artifact для реализованного Python-контракта является явным audit gap и не разрешает создать второе ручное schema-определение.
