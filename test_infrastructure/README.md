# Проверки инфраструктурной композиции

Тесты общего Compose, сохранности данных и операторских механизмов инфраструктуры.

## Основные возможности

- Модульные проверки конфигурации и восстановления.
- Изолированные сценарии отказа.
- Отдельный сохраняющий жизненный тест.

## Запуск и проверки

Из этого каталога: `poetry install --no-root`, затем `poetry run pytest -q`. Реальный жизненный тест запускается отдельно по [руководству](USER_MANUAL.md#реальный-тест) и управляет контейнерами.

Полный clean-room передаёт `--prepare-local-spire`: новая установка получает собственные initial inputs, реальный enrollment и доставку квитанций через публичные CLI. Первый и повторный `up`, затем `status` проверяют все роли `full-local.json` в обоих физических Compose-проектах; core stop/down циклы сохраняют отдельную инкарнацию SPIRE. Это локальная приёмка одного Docker Engine, без утверждения о физических разных узлах или production maintenance scheduler.

`python owner_compose_lifecycle.py --image IMMUTABLE_LOCAL_IMAGE --output PRIVATE_EVIDENCE_ROOT` выполняет отдельную контейнерную квалификацию [общего owner-compose executor](../docs/api/owner-compose.md), не platform E2E. Требуется уже загруженный native Python 3.13.7 Alpine 3.22 image по digest/ID; CI берёт прежний точный base pin из PostgreSQL Dockerfile и явно pull до теста. Создаются две изолированные runtime instances и два вида operation, проверяются repeat, distinct state, config drift, native failure exit и отказ start stopped instance. Исходники/requests/журналы находятся вне checkout, тестовые сети получают явно свободные IPAM subnets, cleanup проверяет собственные labels и исходный baseline. В CI публикуется только verification JSON; raw private diagnostics не загружаются. Физическая многоузловая приёмка отложена в [TODO-MVP](../TODO-MVP.md).

## Навигация

- [USER_MANUAL.md](USER_MANUAL.md) — подробные интерфейсы, условия и ограничения.
- [Родительский каталог](..) — остальной состав поставки.
