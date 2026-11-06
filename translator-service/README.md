# Translator Service

Вспомогательный сервис-посредник перевода. Запросы и результаты передаются через Kafka; сервис не знает предметной области заказчика и передаёт текст непрозрачному провайдеру, не зная, используется ли там модель, словарь, внешний API или человек.

## Основные возможности

- Обычная и приоритетная очереди.
- Сохранение попыток и исходного порядка заданий при повторах.
- Устойчивая публикация результатов.
- Восстановление связи с провайдером с сохранением незавершённых заданий.
- Диагностика и управление через операторский API.
- Собственные метрики, журналы и трассы.

## Запуск

После подготовки по [руководству](USER_MANUAL.md), из этого каталога:

```sh
docker compose --env-file .env config --quiet
docker compose --env-file .env up --build -d --wait --wait-timeout 900
```

Для удалённого Kafka используется `compose.remote.yml`. Основной Compose собирает `translator-core-runtime` без весов, движка, provider lock или реализации перевода. `compose.compute.yml` соединяет разные digest-pinned images внешнего provider и worker; одинаковый digest запрещён. Каноническая реализация provider находится в отдельном private-репозитории [`sapogov1978/AELEV-translation-provider`](https://github.com/sapogov1978/AELEV-translation-provider). Compose не задаёт провайдеру команду, среду или healthcheck. Worker отправляет через приватный Unix socket только текст и языки и получает строку перевода; оператор может заменить provider любой совместимой реализацией, не меняя Kafka-контракты и очередь. В Platform UI публикуется только доступность «Провайдера перевода».

## Навигация

- [architecture.mmd](architecture.mmd) — диаграмма границы, компонентов и основных потоков владельца.
- [src/](src), [alembic/](alembic) — реализация и миграции.
- [compute-runtime/](compute-runtime/README.md) — соединение внешнего provider image и worker.
- [USER_MANUAL.md](USER_MANUAL.md), [SPEC.md](SPEC.md), [ENDPOINTS.md](ENDPOINTS.md) — эксплуатация, поведение и API.
- [test/](test), [secrets/](secrets/README.md) — проверки и описание секретов.
- [INDEX.md](INDEX.md), [TASKS.md](TASKS.md), [CHANGELOG.md](CHANGELOG.md) — агентская карта, готовность и история.
