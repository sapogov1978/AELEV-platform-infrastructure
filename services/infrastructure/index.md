# Инфраструктура DRP: карта каталога

Этот файл является только навигацией. Назначение и состав находятся в [README](README.md), эксплуатация — в [USER_MANUAL](USER_MANUAL.md), нормативная граница — в [SPEC](SPEC.md), локальная история — в [CHANGELOG](CHANGELOG.md).

## Совместная поставка

| Что искать | Где находится |
|---|---|
| Состав и быстрый запуск инфраструктуры | [README.md](README.md) |
| Подготовка, эксплуатация и восстановление | [USER_MANUAL.md](USER_MANUAL.md) |
| Нормативные ограничения | [SPEC.md](SPEC.md) |
| Состояние задач слоя | [TASKS.md](TASKS.md) |
| Статистика файлов, строк и форматов | [STATS.md](STATS.md) |
| Общая модель включения поставок | [infrastructure.mmd](infrastructure.mmd) |
| Корневая Compose-композиция | `compose.yml`, `.env.example` |
| Ограниченное операторское восстановление | [recovery.py](recovery.py), [инструкции](USER_MANUAL.md#ограниченное-восстановление-по-unhealthy) |
| Первоначальная opt-in регистрация точного контейнера | [Публичный вход](USER_MANUAL.md#первоначальная-регистрация-новой-установки), [проверка](test_infrastructure/registration-result-20261024.md) |
| Проверки общей оркестрации | [test_infrastructure/README.md](test_infrastructure/README.md) |

## Самостоятельные продукты

| Возможность | Каталог |
|---|---|
| Межсервисный транспорт | [Kafka](kafka/index.md) |
| Идентификация и доступ | [Keycloak](keycloak/index.md) |
| Удостоверения рабочих нагрузок | [SPIRE](spire/index.md) |
| Объектное хранилище | [MinIO](minio/index.md) |
| Транзакционное хранилище | [PostgreSQL](postgresql/index.md) |
| Контролируемый публичный доступ | [Public Egress Gateway](public-egress-gateway/index.md) |
| Технический перевод | [translator-service](translator-service/index.md) |
| Операторская оболочка | [Platform UI](platform-ui/index.md) |
| Телеметрия и наблюдение | [observability](observability/index.md) |

## Наблюдаемость

| Продукт | Каталог |
|---|---|
| Устойчивая доставка | [delivery](observability/delivery/index.md) |
| OTLP-обработка | [OpenTelemetry Collector](observability/otel-collector/index.md) |
| Метрики и probes | [Prometheus](observability/prometheus/index.md), [Blackbox Exporter](observability/blackbox-exporter/index.md) |
| Журналы и трассы | [Loki](observability/loki/index.md), [Tempo](observability/tempo/index.md) |
| Панели и уведомления | [Grafana](observability/grafana/index.md) |

Настройки, секреты, данные, подготовка, остановка и восстановление принадлежат каждому указанному владельцу и не дублируются в этом индексе. Общая модель развёртывания находится в [docs/architecture/21-deployment.md](https://github.com/sapogov1978/discovery-research-platform/blob/8d1a663319ac15d8729cf1422858f37d57096c75/docs/architecture/21-deployment.md).

Датированные доказательства и границы незавершённых этапов собраны в [общей сверке 21 октября](https://github.com/sapogov1978/discovery-research-platform/blob/8d1a663319ac15d8729cf1422858f37d57096c75/docs/audit/PROVENANCE.md); это не замена текущему состоянию Platform UI.
