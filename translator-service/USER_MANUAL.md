# Руководство translator-service

## Независимое размещение telemetry sender

Дополнительный `compose.telemetry-network.yml` выбирается вместе с `compose.remote.yml` только для нужного sender instance. Его [отдельный env example](deployment/telemetry-network.env.example) перечисляет обязательные новые inputs с prefix `AELEV_INFRASTRUCTURE_TRANSLATOR_`: `TELEMETRY_BIND_ADDRESS`, `TELEMETRY_HTTP_PORT`, `TELEMETRY_CONFIGURATION_ROOT`, `TELEMETRY_DELIVERY_ORIGIN`, `TELEMETRY_SCRAPE_ORIGIN`, `TELEMETRY_SCRAPE_SERVER_NAME`. Origins содержат только HTTPS authority, без credentials/path/query/fragment; scrape получает `/metrics`, Delivery — прежние четыре protocol paths. Bind — явный IP назначенного узла, published port 1–65535 уникален в его inventory, server name — проверяемое SAN. Нет автоматического выбора Docker aliases или endpoints.

Подготовьте собственный закрытый initial env и выполните offline renderer из каталога владельца:

```sh
python deployment/prepare_telemetry_network.py --environment-file /absolute/private/owner.env --output-directory /absolute/private/generated-telemetry
```

Все absolute paths задаёт оператор. Output parent уже существует; fresh output получает directory0700/files0600. Точный repeat сохраняет bytes/mtime, несовпадение/partial/foreign/symlink закрыто отказывается без repair/delete. Вывод содержит только status и SHA-256 файлов. Renderer не читает credential keys, не выполняет сеть, выдачу сертификатов или запуск.

В prepared workflow source `generated-telemetry/sender.local.json` и `generated-telemetry/prometheus.yml` передаются прежним `preparations.private_files` как `secrets/telemetry-configuration/sender.local.json` и `secrets/telemetry-configuration/prometheus.yml`, каждый с mode0640. `TELEMETRY_CONFIGURATION_ROOT` указывает absolute `workspace/secrets/telemetry-configuration` соответствующего instance. SDK сохраняет executor GID: он должен совпадать с существующим `TELEMETRY_SECRET_GID` sender. Не меняйте SDK/UID, не делайте исходники общедоступными и не назначайте группу другой роли для обхода ошибки; несовпадение требует owner identity-delivery handoff.

Translator scrape использует существующий published operator HTTPS `/metrics`: подготовьте отдельные sender-owned `api-ca.crt`, `api-query.crt`, `api-query.key` внутри его собственного telemetry `sender` secret directory. Это отдельная query identity, доверенная operator API; operator/publisher/private keys других ролей не копируются. Private HTTP8096 остаётся локальным.

Recipe добавляет только published4318 и два exact `configs.file` overrides. Public rules, pinned image, UID10001, journal volume, limits/retry/restart, local-write/central-write/local-query и встроенный `https://localhost:4318/api/v1/write` сохраняются. Собственная telemetry-data-init должна завершиться в явном workflow DAG; канонический SDK запускает одну роль с `--no-deps`, не стартует другой runtime или его копию. Profiles не меняют backend routes центральной Delivery и не выдают RESUME.

Проверка подготовленного рабочего env не запускает контейнеры:

```sh
docker compose --env-file .env --file compose.remote.yml --file compose.telemetry-network.yml config --quiet
```

`config` и host tests не доказывают текущий handshake/delivery. Published sender/API edges, mTLS negatives и общая наблюдаемость проверяются manager-owned полным workflow отдельно.

Для независимо размещаемой собственной SQL-роли выберите `compose.remote.yml` и `compose.database-network.yml`. Задайте полный owner namespace `AELEV_INFRASTRUCTURE_TRANSLATOR_`: прежние `DATABASE_IMAGE`, `DATABASE_PORT`, `DATABASE_VOLUME`, `DATABASE_SECRET_ROOT`, `RESTART_POLICY=no`; новые `DATABASE_BIND_ADDRESS`, `DATABASE_SERVER_TLS_ROOT`, `DATABASE_TLS_VOLUME`, `DATABASE_TLS_EXTERNAL`. Bootstrap root содержит только `server.crt`, `server.key`, `client-ca.crt`, regular single-link files `0600/0640`. Одноразовый `translator-database-identity-preparer` использует external=false; runtime — true после его receipt при том же unique volume на назначенном SQL node. Initializer не выпускает/не продлевает leaf и не исправляет чужое или частично записанное состояние. Клиент каждой роли использует свой достижимый `DATABASE_HOST`/published port, `DATABASE_NETWORK_INTERNAL=false`, `DATABASE_SSLMODE=verify-full`, CA, client identity/password; server/client projects и сети могут быть разными. Legacy default не переключается. [Scope, официальные основания и текущие ограничения](database-network-plan-20261120.md).

Независимое размещение provider и worker определяется [00-INFRA-0018](../docs/adr/00-INFRA-20261113-0018-independent-roles-and-translation-mtls.md) и [сетевым руководством](compute-runtime/README.md#независимое-сетевое-размещение). Worker использует явные `AELEV_TRANSLATOR_PROVIDER_TRANSPORT=mtls`, `AELEV_TRANSLATOR_PROVIDER_URL=https://...` и `AELEV_TRANSLATOR_PROVIDER_TLS_ROOT`; в client identity policy `environment` совпадает с `AELEV_ENVIRONMENT`, а production допускает только `spire-file-v1`. TLS/config/transport failures оставляют job в прежнем retry; настройка сети не запускает обработку, не меняет очередь и не даёт `RESUME`.

## Явное сопоставление имени provider

При отсутствии внешнего DNS выберите `compose.compute-worker.mtls.yml`, затем последним `compose.compute-client.hosts.yml` в собственном списке recipes worker. В том же закрытом env задайте `AELEV_INFRASTRUCTURE_TRANSLATOR_PROVIDER_URL` с HTTPS FQDN и опубликованным портом, `AELEV_INFRASTRUCTURE_TRANSLATOR_PROVIDER_DNS_NAME` с точно тем же FQDN и `AELEV_INFRASTRUCTURE_TRANSLATOR_PROVIDER_NODE_ADDRESS` с назначенным IP узла provider. Допускаются IPv4 и IPv6; это адрес provider node, который может отличаться от worker node. FQDN должен присутствовать в запланированном DNS SAN удостоверения provider. Установщик обязан сверить эти значения с выделением endpoints до запуска. `localhost`, wildcard, multicast, неопределённый адрес и `host-gateway` не используются вместо выделенного node IP.

Предварительный render из каталога владельца читает только явно выбранный env:

```sh
docker compose --env-file /operator/worker.env --file compose.compute-worker.mtls.yml --file compose.compute-client.hosts.yml config --quiet
```

Эта команда не запускает контейнеры; полный установщик использует тот же явный порядок файлов и прежние owner procedures. Standalone `station.py --role worker` сам не включает новый файл. Если внешний DNS уже работает, не выбирайте дополнительный profile и новые mapping inputs не требуются. Профиль добавляет только запись в `/etc/hosts` worker; он не меняет URL, CA, SVID, hostname/exact-URI checks, сеть, образ или lifecycle provider. Успех render не подтверждает текущую DNS/TLS-доступность, свежесть SVID, admission или продвижение очереди. Нужны отдельные native проверки. [Нормативные условия и основания](SPEC.md#явное-сопоставление-имени-provider).

## Отдельный образ издателя

`AELEV_INFRASTRUCTURE_TRANSLATOR_UI_IMAGE` выбирает собственную цель `status-runtime`: артефакты и runtime reference provider в неё не входят. Закреплённый `aelev-platform-ui-client==1.1.2` проверяет живость командой `python -S /opt/aelev/publisher-health.py AELEV_INFRASTRUCTURE_TRANSLATOR_`, читая только подтверждённый heartbeat собственного журнала без загрузки приложения и сетевых запросов. Обновляйте только `translator-service-status-publisher`, сохраняя его том; перед этим согласуйте env и отдельные файловые identities с текущим Compose, не копируйте секреты другого владельца и не ослабляйте TLS. Такая проверка не подтверждает доступность провайдера и не устанавливает автовосстановление по `unhealthy`.

Локальный Swagger без установки клиентского сертификата: [полная настройка и команды](USER_MANUAL.md#группы-и-операции-swagger). Откройте `http://127.0.0.1:18496/docs`, нажмите `Authorize` и войдите через существующий Keycloak своим оператором с назначенным `operator-read`. Без собственного токена операции возвращают 401; право оболочки не заменяет право API. HTTP допускается только в явно выбранной локальной установке на loopback, не для удалённого сервера. IdP сохраняет HTTPS, корпоративные ограничения браузера не обходятся.

Общий инфраструктурный компонент выполняет технические задания на перевод и не знает, какой клиент или предметный процесс использует результат. Межсервисные задания и результаты проходят только через принадлежащие translator-service Kafka topics. Операторский Swagger по [00-INFRA-20261106-0012-translator-technical-capability](../docs/adr/00-INFRA-20261106-0012-translator-technical-capability.md) может отправить собственный запрос в ту же очередь и дождаться перевода; частный HTTP backend остаётся только для диагностики.

## Установка и запуск

Исходники, собственная PostgreSQL, миграции, закреплённые схемы, `compose.yml` и `.env.example` находятся в этом каталоге. Команды ниже выполняются из него после подготовки env, файлового пароля и выбранного TLS/OIDC-режима по [README](USER_MANUAL.md#установка-и-запуск). Существующие настройки и секреты не перезаписывайте. Обычный запуск автоматически поднимает Kafka из соседней канонической поставки, ждёт её готовности и применяет topics; вручную запускать provisioner не требуется. Если тома уже заняты корневой оркестрацией, используйте её проект, а не создавайте второго писателя. Секреты не публикуются.

```sh
docker compose --env-file .env config --quiet
docker compose --env-file .env up --build -d --wait --wait-timeout 600
docker compose --env-file .env ps
```

`translator-migrator` выполняется до рабочих ролей и не повторяется бесконечно. `translator-topic-provisioner` ждёт healthy Kafka, при ошибке соединения повторяется и не пропускает consumer, outbox или API дальше. Канонический корневой запуск требует healthy `translator-api`, работающие consumers/outbox и exact provider/worker images. Недоступность провайдера не удаляет задания: они сохраняются для bounded retry.

Если Kafka находится на другом сервере, используйте `compose.remote.yml`: он не запускает Kafka на узле переводчика и проверяет реальный endpoint. Защищённый broker должен уже существовать. Для удалённой PostgreSQL задайте маршрутизируемый `DATABASE_HOST`, `DATABASE_NETWORK_INTERNAL=false`, `DATABASE_SSLMODE=verify-full` и доставьте CA/client certificate/key в отдельный `DATABASE_SECRET_ROOT`. `compose.compute.yml` и `compute-runtime/` размещают совместимый внешний provider image с исходящей сетью и generic read-only secret file; model ID, endpoint и prompt остаются непрозрачными. Root/full-local закрепляет этот recipe exact digests, но production admission реального inference остаётся отдельным.

## Функциональная проверка без провайдера

После подготовки рабочих env/secrets и образов Kafka/Translator по общему [clean-room runbook](../docs/operations/clean-room-reproduction.md) из каталога Translator выполните `poetry install --no-root`, затем `poetry run python test/lifecycle.py --report /var/tmp/translator-acceptance/translator-lifecycle.json`. Это разрушающий только собственные новые тестовые ресурсы сценарий: при прежних контейнерах/томах выбранных проектов он отказывает. Он запускает настоящий Kafka, собственный PostgreSQL и семь управляющих ролей Translator, проверяет сохранение и дедупликацию обеих очередей без провайдера, restart и отказ Kafka, затем удаляет только созданные ресурсы и сверяет baseline. Исполнение и качество перевода этим сценарием не допускаются.

Image preflight учитывает tag и точные repository digests из [Docker image ls --digests](https://docs.docker.com/reference/cli/docker/image/ls/#digests): образ, полученный как `repository@sha256:...` без tag, является полноценным подготовленным входом. Другой digest не заменяет требуемый; lifecycle не скачивает образ самостоятельно.

## Что происходит с заданием

Обычный независимый запрос `TranslationRequestedV2` содержит текст, исходный и целевой языки и техническую идентичность запроса, без модели, её версии или операции исполнения. Срочный запрос публикуется в `translation.requests.priority.v2`, обычный — в `translation.requests.v2`; payload у них одинаков. Consumer сохраняет запрос, worker передаёт текст и языки непрозрачному провайдеру, а outbox публикует `TranslationCompletedV2` без сведений о способе перевода. Новый перевод может отличаться после замены провайдера. Для повторной доставки уже завершённого технического запроса возвращается сохранённый ответ. V1 остаётся только для миграционного завершения старых клиентов.

`PENDING` означает ожидание, `RUNNING` — активную попытку, `SUCCEEDED` — готовый результат, `FAILED` — доказуемую ошибку входа или непригодный ответ провайдера. Временный технический сбой автоматически повторяется и не создаёт отменённой карточки. Готовый перевод ещё не доказывает правильность смысла; итоговую проверку локализации выполняет её владелец.

## Статус, пауза и продолжение

### Порядок очереди и повторы

Планировщик сохраняет исходный возраст технического задания по [SPEC](SPEC.md#вход-и-вычисления). Пауза перед повтором временно делает его недоступным, но не меняет место среди допущенных заданий своего класса. Очередь состоит из отдельных строк, не целых локализаций; готовность полного языка определяет его владелец после получения всех результатов. Более поздний перевод всё ещё может завершиться раньше, если старый находится на паузе или выполняется другой ячейкой.

Для разбора смотрите у самого переводчика `translation_job.created_at`, `available_at`, `state`, `attempt_count` и `error_code`; не сравнивайте только время последнего изменения локализации у клиента. Журнал `translation_provider_retry` содержит тот же ограниченный код и корреляцию без текста. Исторические коды с `MODEL`/`NATIVE` сохраняются для совместимости, но обозначают только классы отказа непрозрачного провайдера и не раскрывают его реализацию. `TRANSLATOR_UNAVAILABLE` обозначает неизвестную причину. Новый код ожидайте только после новой попытки; повтор всей локализации не нужен для исправления порядка уже принятых строк.

Исправление сортировки и кодов не требует новой ревизии БД, RESET, переписывания `created_at`/`available_at`, сброса Kafka offsets или повторного вычисления готовых ответов. После штатного завершения активной попытки и обновления совместимых provider/worker проверьте новый lease среди самых старых допущенных заданий, сохранение возраста и результат через outbox. Сам факт взятия в работу не доказывает успешность перевода.

### Группы и операции Swagger

В `/docs` группы расположены в порядке `default`, `translator`, `metrics`. Операторский режим требует собственной авторизации по [README](ENDPOINTS.md); группировка не меняет права доступа или ответы.

| Группа | Endpoint | Что показывает |
|---|---|---|
| `default` | `GET /healthz` | API отвечает. Это не проверка БД, Kafka или провайдера перевода. |
| `default` | `GET /readyz` | Собственная БД, broker Kafka и шесть topics v1/v2 доступны; при отказе — 503. Доступность провайдера и продвижение заданий этим не подтверждаются. |
| `default` | `GET /v1/components` | Наблюдения присутствия `priority-consumer`, `normal-consumer`, `worker`, `outbox` и `provider`: `state` и время `observed_at`. Устаревшее/отсутствующее наблюдение означает `UNKNOWN` и `observed_at=null`, а не доказанную остановку. |
| `default` | `GET /v1/status` | `control.desired_state` — разрешена ли выдача новых заданий; `revision` — ревизия управления; `priority_streak` — текущая серия срочных выдач. `jobs` содержит числа заданий по состоянию и очереди, например `PENDING:normal`. Отсутствующая комбинация означает отсутствие таких заданий. |
| `translator` | `POST /v1/translate` | Операторский перевод через выбранную очередь Kafka. Требует отдельного `operator-translate`; частный listener не предоставляет этот endpoint. |
| `metrics` | `GET /metrics` | Числа заданий в формате Prometheus: `aelev_translator_jobs` с метками `state` и `queue`. |

`/v1/status` и `/v1/components` полезны вместе: очередь может накапливаться при живых исполнителях, а API может отвечать при отсутствующем worker. Heartbeat означает присутствие, не отсутствие зависания или успешный перевод; внутреннюю готовность определяет сам непрозрачный провайдер. Эти ответы не содержат текстов заданий, готовых переводов или сведений о клиентах и не заменяют итоговую проверку качества перевода.

### Управление выдачей заданий

Команды выполняются только человеком с разрешённым эксплуатационным доступом к инфраструктуре. CLI не является публичным HTTP-интерфейсом и не даёт доступ к чужим хранилищам. Прочитайте состояние и текущую ревизию:

```sh
curl --fail http://127.0.0.1:8096/v1/status
```

Чтобы прекратить выдачу новых заданий, сохраните PAUSE с уникальным идентификатором, своей идентичностью, причиной и ожидаемой ревизией. Активная единица может завершиться. Вместо PAUSE используйте RESUME для продолжения; ожидаемую ревизию берите из нового статуса, а не копируйте прежнее число.

```sh
docker compose --env-file .env exec translator-priority-consumer \
  python -m translator_service.runtime control \
  --action PAUSE --command-id maintenance-1 --actor operator \
  --expected-revision 1 --rationale planned-maintenance
```

RESET возвращает только незавершённую работу в очередь. Сначала выполните PAUSE и дождитесь отсутствия активных аренд, затем отправьте RESET с текущей ревизией, новым идентификатором и причиной; после этого отправьте RESUME. Готовые результаты и история не стираются. Конфликт ревизии требует перечитать статус. Повтор того же идентификатора допустим только с точно тем же телом команды.

## Операторский перевод

Владелец сначала явно включает `API_TRANSLATION_ENABLED=true` и выполняет настройку из [README](USER_MANUAL.md#операторский-перевод). При `false` POST не показывается и недоступен; пять диагностических GET сохраняются. В текущем локальном стенде операторский перевод включён.

Настройте собственные OIDC и пределы по [README](USER_MANUAL.md#операторский-перевод), выполните `translator-api-provision` и войдите через `Authorize`. Настроенному `API_OPERATOR_USER` задача явно назначает два отдельных права; прочие пользователи не получают право перевода автоматически. `operator-read` позволяет читать диагностику, но не отправлять текст; `operator-translate` разрешает POST, но не подменяет право чтения. В HTTPS-режиме клиентского сертификата для POST недостаточно: нужен также собственный токен.

Откройте `translator` → `POST /v1/translate` → `Try it out`, замените тело запроса и нажмите `Execute`:

```json
{
  "source_language": "en",
  "target_language": "de",
  "queue": "normal",
  "text": "Hello, world!"
}
```

`source_language` — код исходного языка, `target_language` — код целевого языка. Коды принадлежат публичному контракту Translator и используют строчные буквы, например `en`, `de`, `ru`; внутренний формат провайдера наружу не выходит. `queue` выбирает только `normal` или `priority`; срочность не означает прерывание уже выполняющегося задания. `text` — точный исходный текст до 12 000 символов, не только пробелы. Для приоритетного задания замените `normal` на `priority`; имя topic, чужой идентификатор и сведения о реализации передавать нельзя.

Ответ 200 содержит перевод в `translated_text`, UUID `request_id`, языки, очередь и предупреждения `warnings`; модель, версия, движок и provider identity отсутствуют. Ответ появляется после полного штатного пути и Kafka ACK результата, не после прямого обращения к провайдеру. Готовый текст не доказывает смысловую точность: проверяйте результат так же, как любой иной перевод. Ограничения непрозрачного порта описаны в [SPEC](SPEC.md).

Если получили 429, API отказал до постановки из-за текущей нагрузки. 422 сообщает неверный ввод или доказуемую ошибку перевода. 503 может означать неподтверждённую доставку, а 504 — превышение времени ожидания; в этих случаях задание могло остаться в очереди и продолжить работу. Отключение браузера его не отменяет. Сохраните `detail.request_id` для диагностики; не воспринимайте ошибку ожидания как доказанную отмену. Каждый новый POST — новое намерение и новое задание, а не идемпотентный повтор; автоматической повторной отправки нет. Точная таблица ошибок и пределы находятся в [контракте](../docs/api/translation.md#операторский-перевод).

## Ограниченное восстановление

Локальные сигналы фоновых ролей проверяют прогресс основного цикла, а не зависимость; окна долгих операций определены в [SPEC](SPEC.md#устойчивый-результат). Для собственной egress-сети sender задаётся `TELEMETRY_EGRESS_SUBNET`: до создания подтвердите отсутствие пересечений с Docker и маршрутами хоста, не меняйте глобальный address pool. В mTLS-листьях обязательны явные Subject Key Identifier и Authority Key Identifier; проверяйте их `openssl verify -x509_strict` и реальным `ssl.create_default_context` Python 3.13, не отключая строгие флаги. Текущая установка и доказательства описаны в [протоколе](test/heartbeat-telemetry-result-20261022.md).

Исходный Compose ограничивает постоянные роли и подготовку topics политикой `on-failure:3`; остальные одноразовые задачи имеют `no`. `AELEV_INFRASTRUCTURE_TRANSLATOR_RESTART_POLICY`, `UI_RESTART_POLICY`, `TELEMETRY_RESTART_POLICY` и `TOPIC_PROVISIONER_RESTART_POLICY` с тем же полным префиксом владельца позволяют сохранить меньший остаток или `no`. Не возвращайте исчерпанному экземпляру исходный максимум. Завершение с кодом 0 и ручная остановка не отменяются автоматическим запуском.

Для восстановления `unhealthy` используется только [существующий оператор инфраструктуры](../USER_MANUAL.md#ограниченное-восстановление-по-unhealthy). Перед обслуживанием сохраните полные stdout/stderr/inspect и прежний журнал ограничителя. Регистрация допускает только живой точный контейнер с пробой; роль без healthcheck или остановленный экземпляр не регистрируются фиктивно. Сначала проверьте причину остановки и явное разрешение на операторский запуск. Не запускайте provider или worker только ради регистрации и не обходите блокировку прежней цели командой `reset`. Отсутствие установленного sender также нельзя выдавать за его регистрацию или работающую доставку телеметрии.

## Диагностика и восстановление

До допуска provider image диагностируйте только принадлежащие Translator БД, Kafka, consumers, outbox и API. Наличие ожидающих `PENDING` заданий при отсутствии провайдера является сохранённой работой, а не отказом инфраструктурного посредника. Поставщик обязан документировать собственные credential, transport, health, capacity, стоимость и проверку качества; один зелёный healthcheck не доказывает корректный перевод.

### Согласованное обновление схемы

Отдельный образ издателя не означает независимость от схемы собственной БД. До обновления сверяйте фактическую `alembic_version`, `schema.HEAD` выбранного выпуска и единственную вершину его Alembic: SQL-роли и издатель должны ожидать один head. Запуск нового издателя поверх прежней БД не является полной выкладкой. Не обходите точный schema gate и не используйте `stamp` вместо DDL.

Сначала сохраните прежние image digests, пароль, имена consumer groups и управляющее состояние. Обновление Translator не выбирает и не обновляет провайдер: его образ выпускает и проверяет отдельный поставщик. Незакреплённый тег отвергается admission. Чистый запуск Translator не зависит от семейства или версии реализации перевода.

Перед сменой схемы прекратите приём/API/outbox и штатно дренируйте worker; активная попытка должна завершиться либо истечь с ограждением. При обслуживании явно задавайте `stop --timeout 120` для API/consumers/outbox и `stop --timeout 250` для worker, проверяйте коды завершения и отсутствие действующих попыток; принудительное завершение не называйте штатным. Если издатель зарегистрирован в host recovery, выполните его `suspend` до обслуживания. Сохранённое `desired_state` не меняйте через RESET. Создайте новый приватный `pg_dump`, восстановите его в отдельной изолированной БД без Kafka и проверьте прежним совместимым `verify-restored-state`. Дополнительно `deployment/check_state.py` сохраняет SHA-256 точных строк всех собственных таблиц, не выводя тексты и credentials. Его диагностическое чтение обеих ревизий не отменяет schema gate рабочего приложения.

Миграцию `0004 → 0005` сначала примените на восстановленной копии. `check_state.py --before <frozen-0004.json> --output <new-0005.json>` разрешает только новый пустой `translation_segment` и смену `alembic_version`, требуя побайтно прежнего содержимого остальных таблиц. Новый `verify-restored-state` должен подтвердить запросы, результаты и outbox. Деструктивные фикстуры Pytest запускайте в другой пустой `translator_test`, не в рабочей БД и не в единственной проверенной восстановленной копии.

Только после репетиции примените тот же migrator к замороженной рабочей БД и повторите сверку до запуска писателей. Затем адресно обновите core-роли одним допущенным digest, а провайдер — своим независимым digest, дождитесь готовности непрозрачного порта, выполните штатную проверку topics без изменения параметров и запустите прежние consumers/outbox/API/worker. Пароль, SQL-права, TLS-режим, Kafka offsets, готовые результаты и управляющее состояние сохраняются. Runtime получает только собственные смонтированные secret-каталоги; `AELEV_INFRASTRUCTURE_TRANSLATOR_SECRET_GID` задаёт дополнительную группу чтения без world-readable файлов или запуска от root.

Завершение требует точного head во всех ролях и БД, успешных `/healthz`/`/readyz`/`/runtimez`, свежих собственных heartbeat и реальной публикации нового состояния в Platform UI, а не только зелёной локальной пробы. Сохраните том/identity/последовательность издателя и явно возобновите его регистрацию в ограничителе после проверки; попытки не обнуляйте автоматически. Для отката после 0005 требуется совместимый образ с поддержкой новой схемы либо отдельное согласованное восстановление: `downgrade` не удаляет checkpoints и не возвращает старых писателей поверх новой ревизии.

```sh
docker compose --env-file .env logs --tail 30 \
  translator-priority-consumer translator-normal-consumer \
  translator-outbox
docker compose --env-file compute-runtime/compute.env \
  -f compose.compute.yml logs --tail 30 translator-worker translation-provider
docker compose --env-file .env exec translator-priority-consumer \
  python -m translator_service.runtime health
```

При недоступности БД consumer не подтверждает Kafka offset и перезапускается; после восстановления он продолжает с сохранённой позиции. Длинный текст разбивается на устойчивые сегменты: после timeout или истечения lease новая совместимая попытка использует уже завершённые checkpoints и продолжает с первого незавершённого сегмента. Сохранённое поле `execution_ref` теперь содержит только внутреннюю provider-neutral область checkpoint и не является именем, версией или выбором реализации. При недоступности непрозрачного порта задание повторяется автоматически, при зависании срабатывает ограниченный timeout. Неподдерживаемый язык или неверный контракт не принимаются за успешный перевод: проверьте ограниченный технический код и доступность порта, не пытайтесь определить или подменить внутреннего исполнителя через Translator. Обычные логи не содержат исходных текстов, SQL или паролей.

Резервную копию своей БД восстанавливайте в отдельный изолированный экземпляр, без доступа к рабочим topics. Сохраните точные схемы и данные Translator; артефакты и состояние провайдера в эту копию не входят. Проверка `python -m translator_service.runtime verify-restored-state` должна вернуть `VERIFIED`; она не исправляет и не публикует данные. Незавершённые аренды после восстановления повторяются через любой совместимый провайдер, готовые результаты сохраняются. Порядок и команды изолированной проверки приведены в [README](README.md).

В local `OTLP_HTTP_ENDPOINT` может быть пустым. Для защищённой доставки отдельные client и sender mTLS-файлы подготавливаются штатным опубликованным оператором наблюдения в собственном `TELEMETRY_SECRET_ROOT`; ключи CA остаются только у оператора. Задайте `OTLP_HTTP_ENDPOINT=https://translator-telemetry-sender:4318` и запустите профиль `telemetry`: он автоматически завершает собственную одноразовую инициализацию тома до sender. Серверное удостоверение включает DNS `translator-telemetry-sender` и loopback для собственного Prometheus. При раздельных CA файл `sender/health-ca.crt` содержит доверие серверу и query identity; центральная write identity подписана уже доверенной центральным приёмником CA. Runtime получает только `client`, sender — только `sender`, без Kafka-admin, database или OIDC-admin secrets. Его Docker-проба `python -S /opt/aelev/delivery_health.py` читает локальный heartbeat, готовность доставки проверяется отдельно по mTLS. При недоступности центрального delivery записи остаются в собственном persistent journal. Проверяйте сроки конечных удостоверений: ручной локальный выпуск на 30 дней не является автоматической production-ротацией.

При адресной совместимой правке четырёх фоновых ролей можно задать `AELEV_INFRASTRUCTURE_TRANSLATOR_BACKGROUND_IMAGE`, сохранив основной `IMAGE` для API. До запуска сравните head, зависимости и mounts; прежние команды, журналы и offsets сохраняются. Новая проба не требует запуска остановленного provider или worker. Для обновления издателя сначала остановите только его, сохраните полный stdout/stderr/inspect и весь собственный том вместе с WAL, затем проверьте изолированную копию новой SQLite через `integrity_check` и сравнение точных строк. Не снимайте старую защитную блокировку и не пополняйте бюджет при замене образа.

Для адресной штатной остановки управляющей части из этого каталога используйте `docker compose --env-file .env stop translator-priority-consumer translator-normal-consumer translator-outbox translator-api`. Вычислительная станция останавливается отдельно командой `sh compute-runtime/bootstrap.sh stop --env-file compute-runtime/compute.env` либо тем же действием PowerShell на Windows и только после подтверждения отсутствия активных lease. Не удаляйте БД, Kafka-журналы, сегментные checkpoints или volumes: остановка процесса не означает удаление истории. Production требует защищённых endpoints и отдельных identities; локальный PLAINTEXT не переносится в production.

## Официальные руководства

- [Apache Kafka 3.9](https://kafka.apache.org/39/documentation/), [consumer groups](https://kafka.apache.org/39/documentation/#intro_consumers) и [delivery semantics](https://kafka.apache.org/39/documentation/#semantics) — очереди, offsets, транзакции и повтор.
- [PostgreSQL 17 transactions](https://www.postgresql.org/docs/17/tutorial-transactions.html) и [libpq TLS](https://www.postgresql.org/docs/17/libpq-ssl.html) — owner-local state и защищённое удалённое подключение.
- [FastAPI deployment concepts](https://fastapi.tiangolo.com/deployment/concepts/) и [OpenAPI](https://fastapi.tiangolo.com/tutorial/metadata/) — диагностический и операторский API.
- [Docker Compose services](https://docs.docker.com/reference/compose-file/services/) — наследование image-owned command, entrypoint, healthcheck, volumes и resource limits.
- [RFC 9110](https://www.rfc-editor.org/rfc/rfc9110.html) — семантика единственного приватного HTTP-запроса к непрозрачному провайдеру.
