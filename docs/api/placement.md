# Полная опись размещения, версия 1

Опись задаёт отдельные экземпляры runtime-ролей, их nodes, immutable artifacts, конфигурации, credentials, состояние и endpoints. `local` и `distributed` используют один контракт. Основание — [00-INFRA-0018](../adr/00-INFRA-20261113-0018-independent-roles-and-translation-mtls.md).

## Интерфейс и граница доказательства

`python3 tools/placement.py validate --inventory PRIVATE_FILE --catalog deployment-catalog.json` проверяет опись без Docker и сетевых вызовов. `plan` возвращает тот же состав и перечень последующих проверок. `--catalog` повторяется для каталогов независимых продуктов; идентификаторы потребителя остаются в его каталоге и не входят в исходники инфраструктуры.

Успех — `VALIDATED_INVENTORY` с `ready_to_start=false`. Это пока проверка плана, не исполняющий полный install workflow интерфейс. Требуются exact release/image admission, владельческий preflight реальных конфигураций и endpoints, подготовка/renewal node identities, проверка recovery handoff и настоящего размещения. Два объявленных узла не доказывают два физических компьютера. Ошибка возвращает exit 2 и `BLOCKED` с code/path без содержимого секрета. Команд `RESUME` здесь нет.

## Каталог ролей

`deployment-catalog.json` содержит `schema_version=1`, `product`, `sources` с относительными путями/SHA-256 и `roles`. Роль задаёт `id`, `owner`, `kind`, `opaque`, `local_state`, `credentials_required`, `recipes`, `configuration_variables`, `compose_profiles`. `runtime` — постоянный процесс; `node` — зависимость среды каждого узла; `operation` — операторская процедура. Названия операций в плане не приказывают выполнить их все: конкретные inputs, полномочия, target node/resource и receipts определяет владельческий workflow. Каталог учитывает overlays/совместимость; он не заменяет их разрешение каноническим Compose.

Пересборка использует dev-only `PyYAML==6.0.3` из `tools/requirements-catalog.txt`, уже применяемый в продукте; validator работает на стандартной библиотеке Python. Команда: `python3 tools/build_deployment_catalog.py --manifest full-local.json --product platform-infrastructure --node-role spire-agent --operation spire-operator --operation bucket-provisioner --operation database-provisioner --operation site-replication-applier --opaque-role translation-provider --output deployment-catalog.json`. `--check` требует побайтного совпадения. Secrets, runtime, tests, examples и obsolete не читаются как источники.

## Поля inventory

| Поле | Значение |
|---|---|
| `schema_version` | Целое `1`, не boolean |
| `mode` | `local` или `distributed` |
| `catalogs` | product → `sha256:` от точных байтов каждого каталога |
| `nodes` | Непустой список назначенных узлов |
| `instances` | Явные экземпляры каждой runtime-роли; для каждой роли нужен running экземпляр |
| `connections` | Направленные ссылки на endpoints, назначение, trust hash и peer identity |

Узел имеет уникальный `id`, явные `address`, `platform=linux/amd64|linux/arm64`, `executor={kind:local|ssh, reference:operator-reference}`, `identity`. Необязательный `aliases` перечисляет дополнительные имена узла. `identity` содержит `server_endpoint=instance:endpoint`, `configuration_ref=config:...`, `configuration_sha256=sha256:...`; authority принадлежит владельцу node identity. Metadata исполнителя не доказывают доступность SSH/Docker или доверие его host key.

`local` требует один узел с local executor; `distributed` — минимум два объявленных узла. Wildcard bind не является advertised address. Loopback и `host.docker.internal` не допускаются как межузловые адреса. Проверка сохраняет выбранный оператором адрес, не заменяя его Docker alias. На каждом узле подготавливаются SPIRE agent и собственная доставка identity; Workload API остаётся локальным, SVID volumes разных узлов не объединяются.

Экземпляр имеет `id`, `role=product/role`, `node`, `configuration_ref`, `configuration_sha256`, массивы `credentials`, `state`, `endpoints` и ровно один `image`/`build`. `image` — registry `name@sha256:...` либо собственный immutable image ID после контролируемой сборки. `build={recipe,target,source_sha256}` ссылается на собственный source recipe; владельческий build/image admission обязателен отдельно. Opaque provider допускает только опубликованный registry digest, без consumer source-build и произвольных command/environment overrides.

Credential содержит только `kind=identity|trust|api-key|database|oidc-client` и `reference=credential:...`. Private identity уникальна для экземпляра; credentials чужого владельца не переиспользуются. Public trust может быть общим. `state` содержит уникальные `state:...` refs собственных изменяемых ресурсов. Для mutable mount и secret mount соответствующие ссылки обязательны. PostgreSQL/Kafka подключаются сетевыми контрактами; shared SQLite/file volume не используется. Validator не читает private keys или runtime databases.

Endpoint имеет `name`, `protocol=https|http|tls|kafka|postgresql`, `url` с явным port, без userinfo/query/fragment и с root path. Схемы — `https`, локальная `http`, `tls`, `kafka+ssl`, `postgresql`. Host соответствует address/alias узла. Issuer/API paths проверяет владелец конфигурации отдельно. Два экземпляра не занимают один порт узла.

Connection содержит `from`, `to=instance:endpoint`, `purpose=domain|technical|operator`, `trust_sha256`, `peer_identity`. Domain transport допускает только Kafka; HTTP — только локальный loopback operator interface. CA не заменяет ожидаемую identity. Наличие нескольких экземпляров требует owner replica admission и само не доказывает fencing/синхронизацию. `desired=running|standby` не является Process Control: живой worker может оставаться `PAUSED`.
