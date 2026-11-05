# Public Egress Gateway: карта каталога

| Что искать | Где находится |
|---|---|
| Назначение, состав и быстрый запуск | [README.md](README.md) |
| Нормативные проверки и режимы отказа | [SPEC.md](SPEC.md) |
| Подключение клиента, grants и диагностика | [USER_MANUAL.md](USER_MANUAL.md) |
| Все HTTP endpoints и права | [ENDPOINTS.md](ENDPOINTS.md) |
| Происхождение и размещение секретов | [secrets/README.md](secrets/README.md) |
| Текущие задачи и допуск площадки | [TASKS.md](TASKS.md) |
| История изменений | [CHANGELOG.md](CHANGELOG.md) |
| Runtime | `gateway.py`, `authorization.py`, `api_contract.py` |
| Операторские утилиты и локальный источник аудита | `prepare.py`, `provision.py`, `renewal.py`, `renewal_schedule.py`, `health.py` |
| Телеметрия владельца | `telemetry.py`, `observability/` |
| Проверки и результаты | [актуальная готовность](test/production-readiness-result.md), [test](test/README.md) |

Архитектурное решение находится в [ADR-0114](../../../docs/adr/20261012-0114-infrastructure-public-egress-gateway.md); разрешённые технические исключения из Kafka-only — в [docs/api/grpc.md](../../../docs/api/grpc.md).
