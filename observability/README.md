# Observability

Слой оркестрации независимых продуктов наблюдаемости DRP. Он собирает поддерживаемые поставки без присвоения их конфигурации, данных и жизненного цикла.

Слой не является отдельным продуктом и не получает собственного статуса готовности. Все семь включаемых продуктов имеют статус `PRODUCTION_READY` по [реестру готовности](https://github.com/sapogov1978/discovery-research-platform/blob/8d1a663319ac15d8729cf1422858f37d57096c75/docs/architecture/19-services-readiness.md#инфраструктурные-продукты).

## Состав

Delivery, OpenTelemetry Collector, Prometheus, Blackbox Exporter, Loki, Tempo и Grafana запускаются как отдельные продукты со своими env, секретами, данными и документацией.

## Быстрый запуск

Подготовьте каждый продукт по [руководству](USER_MANUAL.md), затем выполните:

```sh
if [ ! -f .env ]; then (umask 077; cp .env.example .env); fi
docker compose config --quiet
docker compose up -d --no-build --pull never
docker compose ps --all
```

## Документация

- [observability.mmd](observability.mmd) — кликабельная композиционная диаграмма владельцев наблюдаемости.
- [Агентская карта каталога](INDEX.md)
- [Руководство пользователя](USER_MANUAL.md)
- [Нормативная спецификация](SPEC.md)
- [Задачи слоя и допуск площадки](TASKS.md)
- [История изменений](CHANGELOG.md)
