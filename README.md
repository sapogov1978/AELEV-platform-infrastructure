# AELEV Platform Infrastructure

Самостоятельная целенезависимая поставка технической инфраструктуры: Kafka, PostgreSQL, объектное хранение, идентификация, Platform UI, наблюдаемость, контролируемый сетевой доступ и перевод. Прикладные политики, topics, базы, buckets и полномочия потребителей принадлежат самим потребителям.

## Локальный запуск

Требуются Docker Engine/Desktop с Linux containers, Compose 2.24.4+, Python 3.11+ и OpenSSL. После клонирования этого репозитория выполните:

```sh
./infrastructure up
```

Команда готовит отсутствующую локальную конфигурацию, собирает необходимые образы и запускает PostgreSQL, Kafka, MinIO, Keycloak и Platform UI. UI доступен по `https://127.0.0.1:18082`, имя оператора — `platform-operator`, пароль находится только в локальном исключённом из Git `platform-ui/secrets/provision/operator-password`. Самоподписанное локальное удостоверение требует доверия браузера. Модели, рабочие роли перевода и очереди этим входом не включаются.

```sh
./infrastructure plan
./infrastructure status
./infrastructure down
```

`down` останавливает выбранные инфраструктурные процессы и сохраняет данные. Прикладные продукты останавливаются отдельно. Для ограниченного состава повторяйте `--capability`, например `./infrastructure up --capability kafka`. Состав и зависимости возможностей определяет только [владелец](bootstrap.py); зависимость Platform UI от identity добавляется автоматически. `build` не запускает контейнеры. `init` только подготавливает локальные файлы и проверяет Compose.

Полный `full-local` профиль включает все 15 владельцев, workload identity и наблюдаемость; статус поддерживаемого локального продукта — `PRODUCTION_READY`. [Исправленная программная версия `b1e2ee1`](docs/audit/clean-room-b1e2ee1/RESULT.md) прошла адресные повторные проверки PostgreSQL/root и полную приёмку 11/11 функций с чистым source/Docker baseline, сохраняющей cleanup, независимым verifier и CI 26/26. Пакеты текущего и прежнего evidence сохранены. Машиночитаемый состав, fail-closed preflight, release BOM и воспроизведение из чистого состояния описаны в [clean-room runbook](docs/operations/clean-room-reproduction.md). Новый runtime-выпуск требует собственного CI и полного gate; допуск конкретной production-площадки ведётся отдельно. Реализация перевода не выбрана: Translator сохраняет очередь без провайдера, а локальный процесс, отдельный образ и внешний API остаются вариантами самостоятельного решения.

Быстрый core-запуск предназначен для локального ознакомления. Для production-площадки реальные grants, сертификаты рабочих нагрузок, защищённые endpoints, backup, ресурсный допуск выбранного провайдера и независимые области отказа подготавливаются по руководствам владельцев. Обычный запуск не разрешает возобновление ранее приостановленной обработки.

## Независимые выпуски

Потребитель закрепляет версию, исходный Git commit, SHA-256 ZIP и опись файлов. Пакет формата 2 повторяет корень репозитория: код сервисов, Compose, установщик, документация и инструменты; Graphify и агентские навыки остаются только в исходном репозитории. Рабочие `.env`, ключи, пароли, веса и runtime в пакет не входят. Продукт получает проверенный выпуск автоматически и сохраняет его как локальную зависимость, без редактируемой копии исходников в своём Git.

```sh
python3 tools/build_bundle.py --output dist/AELEV-platform-infrastructure-1.0.0.zip
```

Выпуск 1.0 поставляет закреплённые исходники и рецепты для локальной сборки. Он не объявляется подписанным выпуском готовых production images. [Процедура выпуска](docs/operations/release.md) требует CI, нового полного clean-room evidence точного source commit и проверки ZIP/lock; доказательства опубликованного 1.0 находятся в его release assets. Пакеты и образы следующих выпусков проходят те же проверки целостности, совместимости и допуска; автоматическая загрузка не даёт права обновить уже работающую общую установку.

Закрытый репозиторий требует доступа GitHub при первом получении выпуска. CI потребителя использует отдельный read-only deploy key только к этому репозиторию; личный токен владельца в CI не передаётся.

## Документация и сопровождение

Сервисы расположены прямо в корне: `kafka/`, `postgresql/`, `minio/`, `keycloak/`, `spire/`, `public-egress-gateway/`, `platform-ui/`, `observability/` и `translator-service/`. Здесь же находятся `compose.yml`, `bootstrap.py` и `release.json`; промежуточного `services/infrastructure` в исходниках нет. [00-INFRA-20261106-0002-infrastructure-repository-layout](docs/adr/00-INFRA-20261106-0002-infrastructure-repository-layout.md) описывает новое размещение и совместимость существующих установок.

- [Видение](docs/00-vision.md), [аксиомы](docs/01-axioms.md), [обзор](docs/03-system-overview.md).
- [Агентская карта репозитория](INDEX.md) — высокоуровневый ответ «что и где искать» в дополнение к Graphify.
- [Решение о самостоятельном продукте](docs/adr/00-INFRA-20261105-0001-independent-infrastructure-product.md), [ключевые архитектурные решения](docs/adr/INDEX.md), [происхождение унаследованных ADR](docs/audit/extraction.json).
- [Композиционная диаграмма](infrastructure.mmd), [границы репозиториев](docs/architecture/repositories.mmd), [эксплуатация компонентов](USER_MANUAL.md).
- [Публичный установочный интерфейс](docs/api/installation.md), [изменения](CHANGELOG.md).
- [Карта публичных контрактов](docs/api/INDEX.md), [собственная модель развёртывания](docs/architecture/deployment.md), [supply chain](docs/security/supply-chain.md).
- [Clean-room воспроизведение полного профиля](docs/operations/clean-room-reproduction.md), [машиночитаемый состав](full-local.json).
- [Аудит документации и Production Readiness](docs/audit/production-readiness.md), [официальные источники решений](docs/references/official-sources.md).
- [История прежней композиции](docs/obsolete/services/infrastructure/CHANGELOG.md) и [архив прежнего руководства](docs/obsolete/services/infrastructure/README.md).

Сохранена отдельная identity каждого применимого исторического ADR и цепочки его замены. Ссылки на документы вне состава продукта ведут к конкретному исходному commit, а не к общему индексу. Каждый инфраструктурный сервис сохраняет собственные README, SPEC, contracts, tests и `architecture.mmd`.
