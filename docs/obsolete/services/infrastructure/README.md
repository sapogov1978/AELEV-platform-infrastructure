# Platform Infrastructure — композиция владельцев

Общая композиция самостоятельных технических продуктов. Каждый продукт сохраняет собственные настройки, данные, секреты и ответственность.

## Основные возможности

- Kafka для межсервисных событий.
- PostgreSQL и MinIO для транзакционных и объектных данных.
- Keycloak и SPIRE для человеческих и сервисных удостоверений.
- Контролируемый публичный HTTP(S)-доступ и перевод текстов.
- Platform UI и общий контур наблюдаемости.
- Операторское сопровождение сертификатов и восстановление процессов с паузой после resume, ожиданием Compose-зависимостей, автоматической передачей штатной инкарнации по ADR-0171 и точным переносом истории при согласованной смене Compose-координат.

## Запуск

Для первого локального запуска из чистого checkout используйте корневой `./infrastructure up` либо `python3 bootstrap.py up` из этого каталога. Этот интерфейс готовит выбранные технические возможности и исключает модели и рабочие роли перевода. [Контракт установки](../../../api/installation.md) определяет команды, проверку владения и отдельную остановку общей установки.

После подготовки по [руководству](../../../../USER_MANUAL.md), из этого каталога:

```sh
docker compose --profile ui config --quiet
docker compose --profile ui up -d --no-build --pull never
```

SPIRE выбирается профилем `workload-identity`. Переход с самостоятельных установок описан в руководстве: нельзя запускать второй комплект писателей поверх тех же данных.

## Навигация

- [infrastructure.mmd](../../../../infrastructure.mmd) — кликабельная композиционная диаграмма инфраструктуры.
- [kafka/](../../../../kafka/README.md), [postgresql/](../../../../postgresql/README.md), [minio/](../../../../minio/README.md) — транспорт и хранение.
- [keycloak/](../../../../keycloak/README.md), [spire/](../../../../spire/README.md) — идентификация.
- [public-egress-gateway/](../../../../public-egress-gateway/README.md), [translator-service/](../../../../translator-service/README.md) — сетевой доступ и перевод.
- [platform-ui/](../../../../platform-ui/README.md), [observability/](../../../../observability/README.md) — интерфейс и наблюдение.
- [test_infrastructure/](../../../../test_infrastructure/README.md) — проверки общей композиции.
- [USER_MANUAL.md](../../../../USER_MANUAL.md), [SPEC.md](../../../../SPEC.md) — эксплуатация и границы.
- [INDEX.md](../../../../INDEX.md), [STATS.md](../../../../STATS.md), [CHANGELOG.md](../../../../CHANGELOG.md) — карта, статистика и история.
- [Реестр готовности](https://github.com/sapogov1978/discovery-research-platform/blob/8d1a663319ac15d8729cf1422858f37d57096c75/docs/architecture/19-services-readiness.md#инфраструктурные-продукты) — статусы продуктов.
