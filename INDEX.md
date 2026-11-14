# Platform Infrastructure: карта каталога

Эта агентская карта дополняет Graphify: она показывает высокоуровневую композицию репозитория и отвечает на вопрос «что и где искать». Назначение и состав находятся в [README](README.md), эксплуатация — в [USER_MANUAL](USER_MANUAL.md), нормативная граница — в [SPEC](SPEC.md), локальная история — в [CHANGELOG](CHANGELOG.md).

## Совместная поставка

| Что искать | Где находится |
|---|---|
| Полная опись runtime/node/operator roles и проверка nodes/replicas | [deployment-catalog.json](deployment-catalog.json), [контракт](docs/api/placement.md), [placement.py](tools/placement.py) |
| Состав и быстрый запуск инфраструктуры | [README.md](README.md) |
| Полный штатный runtime и защита инкарнаций | [API установки](docs/api/installation.md#полный-локальный-профиль), [full_runtime.py](tools/full_runtime.py) |
| Первоначальные локальные входы SPIRE и оператор Grafana | [spire_local.py](tools/spire_local.py), [подготовка владельца](spire/USER_MANUAL.md#подготовить-локальный-initial-bundle) |
| Подготовка, эксплуатация и восстановление | [USER_MANUAL.md](USER_MANUAL.md) |
| Нормативные ограничения | [SPEC.md](SPEC.md) |
| Состояние задач слоя | [TASKS.md](TASKS.md) |
| Статистика файлов, строк и форматов | [STATS.md](STATS.md) |
| Общая модель включения поставок | [infrastructure.mmd](infrastructure.mmd) |
| Корневая Compose-композиция | `compose.yml`, `.env.example` |
| Ограниченное операторское восстановление | [recovery.py](recovery.py), [инструкции](USER_MANUAL.md#ограниченное-восстановление-по-unhealthy) |
| Первоначальная opt-in регистрация точного контейнера | [Публичный вход](USER_MANUAL.md#первоначальная-регистрация-новой-установки), [проверка](test_infrastructure/registration-result-20261024.md) |
| Проверки общей оркестрации | [test_infrastructure/README.md](test_infrastructure/README.md) |
| Полный clean-room профиль, preflight, BOM и функциональная приёмка | [full-local.json](full-local.json), [tools/full_local.py](tools/full_local.py), [runbook](docs/operations/clean-room-reproduction.md) |

## Самостоятельные продукты

| Возможность | Каталог |
|---|---|
| Межсервисный транспорт | [Kafka](kafka/INDEX.md) |
| Идентификация и доступ | [Keycloak](keycloak/INDEX.md) |
| Удостоверения рабочих нагрузок | [SPIRE](spire/INDEX.md) |
| Объектное хранилище | [MinIO](minio/INDEX.md) |
| Транзакционное хранилище | [PostgreSQL](postgresql/INDEX.md) |
| Контролируемый публичный доступ | [Public Egress Gateway](public-egress-gateway/INDEX.md) |
| Технический перевод | [translator-service](translator-service/INDEX.md) |
| Операторская оболочка | [Platform UI](platform-ui/INDEX.md) |
| Телеметрия и наблюдение | [observability](observability/INDEX.md) |

## Наблюдаемость

| Продукт | Каталог |
|---|---|
| Устойчивая доставка | [delivery](observability/delivery/INDEX.md) |
| OTLP-обработка | [OpenTelemetry Collector](observability/otel-collector/INDEX.md) |
| Метрики и probes | [Prometheus](observability/prometheus/INDEX.md), [Blackbox Exporter](observability/blackbox-exporter/INDEX.md) |
| Журналы и трассы | [Loki](observability/loki/INDEX.md), [Tempo](observability/tempo/INDEX.md) |
| Панели и уведомления | [Grafana](observability/grafana/INDEX.md) |

Настройки, секреты, данные, подготовка, остановка и восстановление принадлежат каждому указанному владельцу и не дублируются в этом индексе. Общая модель развёртывания находится в [собственной модели развёртывания](docs/architecture/deployment.md).

Датированные доказательства, допуск опубликованного 1.1.3 и границы незавершённых этапов собраны в [общем аудите](docs/audit/production-readiness.md); текущее состояние работающей установки показывает Platform UI.
