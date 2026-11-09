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
текст и языки и получает строку перевода. Root/full-local запускает отдельные
digest-pinned provider/worker images через `compose.compute.yml`, но не публикует
provider как собственный компонент Translator в Platform UI. Recipe предоставляет
исходящую сеть, UDS и generic secret mount, не зная model ID, endpoint или prompt;
само включение образов не является production admission реального inference.

## Навигация

- [architecture.mmd](architecture.mmd) — диаграмма границы, компонентов и основных потоков владельца.
- [src/](src), [alembic/](alembic) — реализация и миграции.
- [compute-runtime/](compute-runtime/README.md) — самостоятельный lifecycle той же provider/worker-ячейки, которую root/full-local включает канонически.
- [USER_MANUAL.md](USER_MANUAL.md), [SPEC.md](SPEC.md), [ENDPOINTS.md](ENDPOINTS.md) — эксплуатация, поведение и API.
- [test/](test), [secrets/](secrets/README.md) — проверки и описание секретов.
- [INDEX.md](INDEX.md), [TASKS.md](TASKS.md), [CHANGELOG.md](CHANGELOG.md) — агентская карта, готовность и история.
