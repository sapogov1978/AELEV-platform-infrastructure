# translator-service: карта каталога

| Что искать | Где находится |
|---|---|
| Назначение, состав и быстрый запуск | [README.md](README.md) |
| Нормативные гарантии | [SPEC.md](SPEC.md) |
| Установка, Swagger, управление и восстановление | [USER_MANUAL.md](USER_MANUAL.md) |
| Все HTTP endpoints и права | [ENDPOINTS.md](ENDPOINTS.md) |
| Текущие задачи и допуск площадки | [TASKS.md](TASKS.md) |
| История изменений | [CHANGELOG.md](CHANGELOG.md) |
| Backend и миграции | `src/translator_service/`, `alembic/` |
| Согласованное обновление и сверка сохранности | [процедура](USER_MANUAL.md#согласованное-обновление-схемы), `deployment/check_state.py` |
| Проверенная выкладка схемы 0005 на стенд | [результат 18 октября](test/schema-0005-rollout-result-20261018.md) |
| Порядок технических повторов и диагностика | [результат проверки и выкладки](test/stable-retry-result-20261018.md) |
| Телеметрия владельца | `src/translator_service/observability.py`, `observability/` |
| Локальная и удалённая управляющие топологии | `compose.local.yml`, `compose.remote.yml`, `compose.yml` |
| Самостоятельная вычислительная станция | [compute-runtime/README.md](compute-runtime/README.md), `compose.compute.yml` |
| Закреплённая модель | `translator.lock.json`, `download-model.py` |
| Проверки и сравнение моделей | [актуальная проверка готовности](test/production-readiness-result.md), `test/` |

Kafka-контракт находится в [docs/api/translation.md](../../../docs/api/translation.md), инфраструктурная классификация — в [ADR-0087](../../../docs/adr/20261008-0087-infrastructure-translator-service.md).
