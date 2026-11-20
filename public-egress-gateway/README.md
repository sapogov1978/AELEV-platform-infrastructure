# Public Egress Gateway

## Независимое размещение telemetry sender

Дополнительный `compose.telemetry-network.yml` выбирается вместе с `compose.yml` только для нужного sender instance. Его [отдельный env example](deployment/telemetry-network.env.example) перечисляет обязательные новые inputs с prefix `AELEV_INFRASTRUCTURE_PUBLIC_EGRESS_GATEWAY_`: `TELEMETRY_BIND_ADDRESS`, `TELEMETRY_HTTP_PORT`, `TELEMETRY_CONFIGURATION_ROOT`, `TELEMETRY_DELIVERY_ORIGIN`, `TELEMETRY_SCRAPE_ORIGIN`, `TELEMETRY_SCRAPE_SERVER_NAME`. Origins содержат только HTTPS authority, без credentials/path/query/fragment; scrape получает `/metrics`, Delivery — прежние четыре protocol paths. Bind — явный IP назначенного узла, published port 1–65535 уникален в его inventory, server name — проверяемое SAN. Нет автоматического выбора Docker aliases или endpoints.

Подготовьте собственный закрытый initial env и выполните offline renderer из каталога владельца:

```sh
python deployment/prepare_telemetry_network.py --environment-file /absolute/private/owner.env --output-directory /absolute/private/generated-telemetry
```

Все absolute paths задаёт оператор. Output parent уже существует; fresh output получает directory0700/files0600. Точный repeat сохраняет bytes/mtime, несовпадение/partial/foreign/symlink закрыто отказывается без repair/delete. Вывод содержит только status и SHA-256 файлов. Renderer не читает credential keys, не выполняет сеть, выдачу сертификатов или запуск.

В prepared workflow source `generated-telemetry/sender.local.json` и `generated-telemetry/prometheus.yml` передаются прежним `preparations.private_files` как `secrets/telemetry-configuration/sender.local.json` и `secrets/telemetry-configuration/prometheus.yml`, каждый с mode0640. `TELEMETRY_CONFIGURATION_ROOT` указывает absolute `workspace/secrets/telemetry-configuration` соответствующего instance. SDK сохраняет executor GID: он должен совпадать с существующим `TELEMETRY_SECRET_GID` sender. Не меняйте SDK/UID, не делайте исходники общедоступными и не назначайте группу другой роли для обхода ошибки; несовпадение требует owner identity-delivery handoff.

Gateway scrape использует прежний `/run/secrets/gateway/server-ca.pem` и server name; его read-only HTTPS `/metrics` не требует fetch key или grant. На sender-узел доставляется только этот публичный CA, не server/fetch private key.

Recipe добавляет только published4318 и два exact `configs.file` overrides. Public rules, pinned image, UID10001, journal volume, limits/retry/restart, local-write/central-write/local-query и встроенный `https://localhost:4318/api/v1/write` сохраняются. Собственная telemetry-data-init должна завершиться в явном workflow DAG; канонический SDK запускает одну роль с `--no-deps`, не стартует другой runtime или его копию. Profiles не меняют backend routes центральной Delivery и не выдают RESUME.

Проверка подготовленного рабочего env не запускает контейнеры:

```sh
docker compose --env-file .env --file compose.yml --file compose.telemetry-network.yml config --quiet
```

`config` и host tests не доказывают текущий handshake/delivery. Published sender/API edges, mTLS negatives и общая наблюдаемость проверяются manager-owned полным workflow отдельно.

Контролируемый HTTP(S)-доступ к публичной сети по собственной identity и разрешению клиента.

## Основные возможности

- mTLS, точные разрешения и квоты.
- Блокировка внутренних адресов и DNS rebinding.
- Ограниченный ответ и собственная телеметрия.

## Запуск

После подготовки по [руководству](USER_MANUAL.md), из этого каталога:

```sh
docker compose config --quiet
docker compose up -d --no-build --pull never
```

Первоначальная сборка, обновление и восстановление описаны в руководстве. При работе через общую инфраструктурную композицию не запускайте отдельный проект поверх тех же данных.

## Навигация

- [architecture.mmd](architecture.mmd) — диаграмма границы, компонентов и основных потоков владельца.
- [USER_MANUAL.md](USER_MANUAL.md), [SPEC.md](SPEC.md) — использование и правила продукта.
- [INDEX.md](INDEX.md), [TASKS.md](TASKS.md), [CHANGELOG.md](CHANGELOG.md) — агентская карта, ограничения и история.
