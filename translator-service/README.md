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

Для удалённого Kafka используется `compose.remote.yml`. Основной Compose
собирает `translator-core-runtime` без весов, движка, provider lock или
реализации перевода. Worker передаёт совместимому непрозрачному порту только
текст и языки и получает строку перевода. Поэтому `full-local` не запускает
provider/worker-ячейку и не публикует provider как собственный компонент
Platform UI. `compose.compute.yml` — необязательный recipe выбранного внешнего
provider image: он предоставляет исходящую сеть, UDS и generic secret mount,
но не знает model ID, endpoint или prompt и не является production admission.

## Навигация

- [architecture.mmd](architecture.mmd) — диаграмма границы, компонентов и основных потоков владельца.
- [src/](src), [alembic/](alembic) — реализация и миграции.
- [compute-runtime/](compute-runtime/README.md) — необязательный recipe внешнего provider image, не входящий в `full-local`.
- [USER_MANUAL.md](USER_MANUAL.md), [SPEC.md](SPEC.md), [ENDPOINTS.md](ENDPOINTS.md) — эксплуатация, поведение и API.
- [test/](test), [secrets/](secrets/README.md) — проверки и описание секретов.
- [INDEX.md](INDEX.md), [TASKS.md](TASKS.md), [CHANGELOG.md](CHANGELOG.md) — агентская карта, готовность и история.
