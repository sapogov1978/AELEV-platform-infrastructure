# Observability: карта каталога

Эта агентская карта дополняет Graphify: она показывает высокоуровневую композицию observability и отвечает на вопрос «что и где искать».

Этот каталог является слоем композиции самостоятельных продуктов, а не дополнительным владельцем телеметрии.

| Что искать | Где находится |
|---|---|
| Состав слоя и совместный запуск | [README.md](README.md) |
| Подготовка, эксплуатация и восстановление | [USER_MANUAL.md](USER_MANUAL.md) |
| Нормативная граница слоя | [SPEC.md](SPEC.md) |
| Состояние задач слоя и допуск площадки | [TASKS.md](TASKS.md) |
| История изменений | [CHANGELOG.md](CHANGELOG.md) |
| Устойчивая доставка | [delivery/INDEX.md](delivery/INDEX.md) |
| OTLP-обработка | [otel-collector/INDEX.md](otel-collector/INDEX.md) |
| Метрики | [prometheus/INDEX.md](prometheus/INDEX.md), [blackbox-exporter/INDEX.md](blackbox-exporter/INDEX.md) |
| Журналы и трассы | [loki/INDEX.md](loki/INDEX.md), [tempo/INDEX.md](tempo/INDEX.md) |
| Панели и уведомления | [grafana/INDEX.md](grafana/INDEX.md) |

Границы самостоятельных продуктов и устойчивой доставки определены [00-INFRA-20261106-0009-independent-observability-and-telemetry](../docs/adr/00-INFRA-20261106-0009-independent-observability-and-telemetry.md).
