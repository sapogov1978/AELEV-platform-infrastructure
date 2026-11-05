# Observability: карта каталога

Этот каталог является слоем композиции самостоятельных продуктов, а не дополнительным владельцем телеметрии.

| Что искать | Где находится |
|---|---|
| Состав слоя и совместный запуск | [README.md](README.md) |
| Подготовка, эксплуатация и восстановление | [USER_MANUAL.md](USER_MANUAL.md) |
| Нормативная граница слоя | [SPEC.md](SPEC.md) |
| Состояние задач слоя и допуск площадки | [TASKS.md](TASKS.md) |
| История изменений | [CHANGELOG.md](CHANGELOG.md) |
| Устойчивая доставка | [delivery/index.md](delivery/index.md) |
| OTLP-обработка | [otel-collector/index.md](otel-collector/index.md) |
| Метрики | [prometheus/index.md](prometheus/index.md), [blackbox-exporter/index.md](blackbox-exporter/index.md) |
| Журналы и трассы | [loki/index.md](loki/index.md), [tempo/index.md](tempo/index.md) |
| Панели и уведомления | [grafana/index.md](grafana/index.md) |

Общая модель наблюдаемости определена [ADR-0099](../../../docs/adr/20261009-0099-consumer-owned-observability.md), устойчивая доставка — [ADR-0100](../../../docs/adr/20261009-0100-durable-owner-telemetry-delivery.md).
