# 00-INFRA-20261106-0009-independent-observability-and-telemetry — Самостоятельная наблюдаемость и устойчивая телеметрия

- **Статус:** Принят
- **Дата:** 2026-11-06
- **Происхождение:** неизменяемые ADR-0099, ADR-0100 и ADR-0107 из [архива выделения](../audit/inherited-adrs/INDEX.md)

## Контекст

Logs, metrics и traces должны доставляться устойчиво, но общий слой наблюдения не должен владеть предметным смыслом, панелями или runtime других сервисов.

## Ключевые решения

- Telemetry Delivery, OpenTelemetry Collector, Prometheus, Blackbox Exporter, Loki, Tempo и Grafana являются семью самостоятельными владельцами.
- Родительский `observability` только компонует их рецепты и не создаёт общий runtime, journal, identity или database.
- Технический транспорт использует OTLP и owner-scoped mTLS identities.
- Устойчивая доставка имеет собственный журнал, acknowledgement, bounded retry и неизменяемый архив принятой истории.
- Prometheus/Loki/Tempo/Grafana являются проекциями и интерфейсами; они не заменяют исходный журнал или backup.
- Конкретные metrics, log labels, spans, alerts и dashboards принадлежат их владельцу-производителю.
- Отказ одного backend не должен блокировать независимую доставку в остальные destinations.

## Последствия

- Наблюдаемость масштабируется и восстанавливается по владельцам.
- Общая установка не получает каталог предметных dashboards или targets.
- End-to-end durability подтверждается локальным журналом и квитанциями, а не только успешным OTLP response.

## Проверка и откат

Тесты проверяют изоляцию Compose, очереди, replay, duplicate acceptance, independent destinations и восстановление журналов. Откат backend сохраняет исходный архив и не сбрасывает publisher sequence.

## Официальные основания

- [OTLP specification](https://opentelemetry.io/docs/specs/otlp/) определяет transport, acknowledgements, retries и multi-destination behavior.
- [Prometheus storage](https://prometheus.io/docs/prometheus/latest/storage/) описывает локальную metrics-проекцию и её ограничения.
- Официальные руководства [Loki](https://grafana.com/docs/loki/latest/), [Tempo](https://grafana.com/docs/tempo/latest/) и [Grafana](https://grafana.com/docs/grafana/latest/) описывают соответствующие технические projections и UI.
