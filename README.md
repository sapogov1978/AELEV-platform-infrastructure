# AELEV Platform Infrastructure

Самостоятельная целенезависимая поставка технической инфраструктуры: Kafka, PostgreSQL, объектное хранение, идентификация, Platform UI, наблюдаемость, контролируемый сетевой доступ и перевод. Прикладные политики, topics, базы, buckets и полномочия потребителей принадлежат самим потребителям.

## Локальный запуск

Требуются Docker Engine/Desktop с Linux containers, Compose 2.24.4+, Python 3.11+ и OpenSSL. После клонирования этого репозитория выполните:

```sh
./infrastructure up
```

Команда готовит отсутствующую локальную конфигурацию, собирает необходимые образы и запускает PostgreSQL, Kafka, MinIO, Keycloak, Platform UI и capability `translation`. Для перевода она получает только два подписанных digest-pinned image — непрозрачный provider и provider-neutral worker — и импортирует существующий `../TranslationProvider/.env.local` в локальный игнорируемый secret mount без вывода значения. Репозиторий провайдера при старте не клонируется. UI доступен по `https://127.0.0.1:18082`, имя оператора — `platform-operator`, пароль находится только в локальном исключённом из Git `platform-ui/secrets/provision/operator-password`. Самоподписанное локальное удостоверение требует доверия браузера.

```sh
./infrastructure plan
./infrastructure status
./infrastructure down
```

`down` останавливает выбранные инфраструктурные процессы и сохраняет данные. Прикладные продукты останавливаются отдельно. Для ограниченного состава повторяйте `--capability`, например `./infrastructure up --capability kafka`. Состав и зависимости возможностей определяет только [владелец](bootstrap.py); зависимость Platform UI от identity добавляется автоматически. `build` не запускает контейнеры. `init` только подготавливает локальные файлы и проверяет Compose.

Полный `full-local` профиль 1.1.3 включает все 15 владельцев, workload identity, наблюдаемость и выбранную пару подписанных provider/worker images. Provider владеет OpenAI Responses API, model lock и предметно-сохраняющим prompt; Infrastructure видит только opaque UDS-контракт и generic secret file. Опубликованный [выпуск 1.1.2](https://github.com/sapogov1978/AELEV-platform-infrastructure/releases/tag/v1.1.2) прошёл CI и 12/12 функций на точном source commit. Кандидат 1.1.3 дополнительно сохраняет выбранный native `no` SPIRE и владельцев восстановленных файлов. Для его допуска обязательны собственные CI, 12/12 функций, независимый verifier и проверка release assets; доказательства прежнего выпуска не переносятся. Машиночитаемый состав, fail-closed preflight, release BOM и воспроизведение из чистого состояния описаны в [clean-room runbook](docs/operations/clean-room-reproduction.md). Допуск конкретной production-площадки ведётся отдельно.

Быстрый core-запуск предназначен для локального ознакомления. Для production-площадки реальные grants, сертификаты рабочих нагрузок, защищённые endpoints, backup, ресурсный допуск выбранного провайдера и независимые области отказа подготавливаются по руководствам владельцев. Обычный запуск не разрешает возобновление ранее приостановленной обработки.

## Независимые выпуски

Потребитель закрепляет версию, исходный Git commit, SHA-256 ZIP и опись файлов. Пакет формата 2 повторяет корень репозитория: код сервисов, Compose, установщик, документация и инструменты; Graphify и агентские навыки остаются только в исходном репозитории. Рабочие `.env`, ключи, пароли, веса и runtime в пакет не входят. Продукт получает проверенный выпуск автоматически и сохраняет его как локальную зависимость, без редактируемой копии исходников в своём Git.

```sh
python3 tools/build_bundle.py --output dist/AELEV-platform-infrastructure-1.1.3.zip
```

Кандидат 1.1.3 поставляет закреплённые исходники и прежние подписанные provider/worker image digests. [Процедура выпуска](docs/operations/release.md) допускает стабильную публикацию только после CI, полного evidence точного source commit и проверки ZIP/lock. Доказательства опубликованного 1.1.2 остаются в его release assets; автоматическая загрузка не даёт права обновить уже работающую общую установку.

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
