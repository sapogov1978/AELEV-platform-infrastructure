# Translator Service

Вспомогательный сервис перевода текстов локальной моделью. Запросы и результаты передаются через Kafka; сервис не знает предметной области заказчика, а операторская страница показывает вычислительную модель как отдельный компонент без привязки интерфейса к семейству модели.

## Основные возможности

- Обычная и приоритетная очереди.
- Сохранение попыток и исходного порядка заданий при повторах.
- Устойчивая публикация результатов.
- Восстановление вычислителя с сохранением незавершённых заданий.
- Диагностика и управление через операторский API.
- Собственные метрики, журналы и трассы.

## Запуск

После подготовки по [руководству](USER_MANUAL.md), из этого каталога:

```sh
docker compose --env-file .env config --quiet
docker compose --env-file .env up --build -d --wait --wait-timeout 900
```

Для удалённого Kafka используется `compose.remote.yml`. По [ADR-0170](../docs/adr/20261028-0170-uniform-owner-compute-station-entry.md) основной Compose больше не содержит compute-профиля, model/worker или их socket volume. Эти внутренние процессы имеют единственный самодостаточный рецепт `compose.compute.yml` и одинаковые `doctor`, `prepare`, `start`, `status`, `stop` для macOS/Linux и Windows в [compute-runtime](compute-runtime/README.md). Станция получает маршрутизируемый доступ только к собственной PostgreSQL переводчика и Telemetry Delivery, а model общается с worker через локальный Unix socket. В Platform UI они представлены одной «Вычислительной моделью», готовой только при готовности обоих процессов; раздельные наблюдения остаются в `/v1/components`. На управляющей стороне `AELEV_INFRASTRUCTURE_TRANSLATOR_COMPUTE_DESIRED_STATE=STOPPED` является явным владельческим намерением и публикует этот единый компонент как `OFFLINE`; перед запуском ячейки значение меняется на `RUNNING`, после чего готовность определяется только свежими heartbeat.

## Навигация

- [architecture.mmd](architecture.mmd) — диаграмма границы, компонентов и основных потоков владельца.
- [src/](src), [alembic/](alembic) — реализация и миграции.
- [translator.lock.json](translator.lock.json) — выбранная модель.
- [compute-runtime/](compute-runtime/README.md) — единый запуск отдельной вычислительной станции.
- [USER_MANUAL.md](USER_MANUAL.md), [SPEC.md](SPEC.md), [ENDPOINTS.md](ENDPOINTS.md) — эксплуатация, поведение и API.
- [test/](test), [secrets/](secrets/README.md) — проверки и описание секретов.
- [index.md](index.md), [TASKS.md](TASKS.md), [CHANGELOG.md](CHANGELOG.md) — карта, готовность и история.
