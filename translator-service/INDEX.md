# translator-service: карта каталога

Эта агентская карта дополняет Graphify высокоуровневым ответом на вопрос «что и где искать» в каталоге владельца.

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
| Независимый sender endpoint и private configs | `compose.telemetry-network.yml`, `deployment/prepare_telemetry_network.py`, `deployment/telemetry-network.env.example` |
| Собственная mTLS API query identity издателя | `compose.publisher-network.yml`, `deployment/publisher-network.env.example`, `test/test_status_network.py` |
| Локальная и удалённая управляющие топологии | `compose.local.yml`, `compose.remote.yml`, `compose.yml` |
| Контракт внешнего провайдера | [docs/api/translation.md](../docs/api/translation.md), [SPEC.md](SPEC.md); реализация остаётся непрозрачной |
| Compute-рецепт | [compute-runtime/README.md](compute-runtime/README.md), `compose.compute.yml` для совместимости; независимые `compose.compute-provider.mtls.yml` / `compose.compute-worker.mtls.yml` выбираются через `--role` и не требуют общего socket-тома |
| Реализация, модель и версия провайдера | В этом репозитории отсутствуют; принадлежат выбранному поставщику |
| Проверки совместимости провайдера | [актуальная проверка готовности](test/production-readiness-result.md), `test/` |
| Сквозной сетевой queue/replay proof | [network queue qualification](test/network-queue-qualification.md), `test/network_queue_lifecycle.py` |

Kafka-контракт находится в [docs/api/translation.md](../docs/api/translation.md), инфраструктурная классификация — в [00-INFRA-20261106-0012-translator-technical-capability](../docs/adr/00-INFRA-20261106-0012-translator-technical-capability.md).
