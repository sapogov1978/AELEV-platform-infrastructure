# Подключение к Public Egress Gateway

## Локальная проба издателя

Закреплённый `aelev-platform-ui-client==1.1.2` проверяет живость издателя командой `python -S publisher-health.py AELEV_INFRASTRUCTURE_PUBLIC_EGRESS_GATEWAY_`, читая только подтверждённый heartbeat собственного журнала без загрузки приложения и сетевых запросов. Собственная готовность gateway проверяется отдельно по каноническому JSON с ограниченным чтением ответа. Обновление только `public-egress-gateway-status-publisher` сохраняет его том и не перезапускает gateway. Автовосстановление по `unhealthy` этим не устанавливается.

## Подготовка провайдера

Создайте `.env` из `.env.example` без перезаписи существующего файла, подготовьте отдельные серверную и workload PKI и доставьте runtime только `server.pem`, `server.key`, `server-ca.pem` и `client-ca.pem` по правилам [каталога секретов](secrets/README.md). Закрытые ключи issuer и credentials клиентов в контейнер не передаются. Затем выполните `docker compose config --quiet`, соберите образ и запустите `docker compose up -d --no-build --pull never`; готовность проверяется штатными health/readiness endpoints без `verify=False` и `curl -k`.

## Телеметрия провайдера

Используйте собственные файлы из [описи секретов](secrets/README.md), выданные утверждённым PKI-оператором. Для локального стенда подходит опубликованный `aelev-observability-tools` с отдельно одобренным issuer, не закрытые ключи соседнего runtime. CA выпуска не монтируется ни gateway, ни sender; gateway/workload CA публичного fetch для этой задачи не меняется. Локальная выдача не заменяет production SPIRE и допуск площадки.

После доставки своего набора задайте `OTLP_HTTP_ENDPOINT=https://public-egress-gateway-telemetry-sender:4318` с полным префиксом владельца и запустите `docker compose --profile telemetry up -d --no-build --pull never public-egress-gateway-telemetry-sender`. Зависимости сначала подготавливают только собственный том и запускают gateway. При обновлении уже зарегистрированного контейнера используйте адресную процедуру с сохранённым бюджетом, а не безусловный общий `up`: `RESTART_POLICY`, `UI_RESTART_POLICY` и `TELEMETRY_RESTART_POLICY` с тем же префиксом должны сохранять фактический остаток либо `no`.

Docker читает только локальный heartbeat sender. Отдельно проверьте HTTPS/mTLS `/healthz`, `/readyz`, `/status` и `/metrics` на порту 13134 его собственной сети с local-query identity. `health-ca.crt` доверяет как серверу, так и issuer диагностического клиента. Нулевая задолженность не доказывает применение в конечном продукте: по [контракту доставки](../docs/api/telemetry.md) отдельно подтвердите исходные метрики и конкретные технические log/span в существующих Prometheus/Loki/Tempo через Grafana. Новый канал SMTP не создаётся. Перед обновлением непустого тома требуются полная остановленная копия и проверка нового SDK по [руководству delivery](../observability/delivery/USER_MANUAL.md#обновление-журнала-и-диагностика).

## Swagger и группы endpoints


Операторская документация публикуется владельцем на `DOCS_PUBLIC_URL`; полный перечень маршрутов и прав находится в [ENDPOINTS.md](ENDPOINTS.md). Группы OpenAPI — `default`, `gateway`, `metrics`. Документация не принимает клиентский ключ и не превращает браузер в клиента защищённого `POST /v1/fetch`; технический вызов выполняется только с отдельной mTLS/SPIFFE identity и действующим grant.

## 1. Что предоставить провайдеру

Клиенту нужны точный HTTPS endpoint, доверенный серверный CA и отдельная клиентская identity. Она должна иметь URI SAN SPIFFE и быть подписана допустимым workload CA этой среды. Сертификат сам по себе не даёт права fetch. Клиент никогда не получает `server.key`, приватный ключ CA, рабочие credentials издателя состояния или доступ на запись в каталог разрешений.

Публичные корни и endpoint доставляет оператор провайдера; identity и ключ — утверждённая PKI/SPIRE и защищённое хранилище владельца. Локальная подготовка показана [выше](#подготовка-провайдера). Не используйте клиентскую identity другого сервиса и не отключайте проверку CA/hostname при смене IP. URI задаётся реальным экземпляром, не зашитым списком в gateway.

## 2. Декларация прав у владельца

Ниже нейтральный пример, который владелец сохраняет у себя, а не в исходниках провайдера. Замените URI своим, `expires_at` — ближайшим согласованным временем UTC. В примере время задаётся для воспроизводимого объяснения структуры, не является рекомендацией долгоживущих прав:

```json
{
  "schema_version": 1,
  "identity": "spiffe://example.test/workload/client-a",
  "expires_at": "2026-10-13T12:00:00+00:00",
  "mode": "EXPLORATION",
  "max_response_bytes": 1048576,
  "timeout_seconds": 5,
  "max_concurrency": 4,
  "per_host_interval_seconds": 0.25,
  "credential_scopes": {},
  "post_scopes": {}
}
```

`identity` привязывает права к точному TLS URI; `expires_at` прекращает новые запросы после срока; `mode` определяет read-only либо точечно разрешённый regular-доступ. `max_response_bytes` ограничивает ответ, `timeout_seconds` — upstream I/O, `max_concurrency` — одновременные fetch этой identity на реплику, `per_host_interval_seconds` — частоту к одному host. Все значения ограничены глобальными пределами провайдера. Ноль, bool вместо числа, NaN/Infinity, неизвестные и повторяющиеся поля не принимаются.

Для REGULAR владелец меняет режим и задаёт `credential_scopes` вида `{"authorization":["api.example.org"]}` и `post_scopes` вида `{"api.example.org":["/session"]}`. Это точные hosts и пути, не wildcard, regex или список допустимых private-сетей. Секретный заголовок не разрешает произвольный POST, а разрешённый POST не разрешает произвольный credential-header. Внешние секреты в JSON разрешения не записываются. Области конкретного продукта остаются только в его декларации.

## 3. Одобрение, применение и отзыв

Оператор проверяет назначение, срок, scopes и суммарную нагрузку; рабочий клиент не может одобрить себя. На узле провайдера выполните его собственную утилиту с точным одобренным входом владельца:

```sh
python3 provision.py check /path/to/owner/grant.json
mkdir -p grants audit
chmod 700 audit
python3 provision.py apply /path/to/owner/grant.json --directory grants \
  --audit-directory audit --actor <operator-subject>
```

Первая команда только проверяет. Вторая создаёт или заменяет один файл с хешем identity в имени. До изменения она устойчиво фиксирует намерение в отдельном `audit/operator-audit.jsonl`, после изменения — завершение с тем же event ID; Actor берётся из подтверждённой операторской identity, а не из имени grant. Право записи имеет только оператор; контейнер получает каталог grants read-only и не получает audit. Новый запрос перечитывает файл, restart не требуется. Повтор не создаёт второго разрешения. Проверить всю хеш-цепочку можно командой `python3 provision.py verify-audit audit/operator-audit.jsonl`; повреждённая цепочка или недоступность записи intent запрещают apply/revoke. Если команда вернула ошибку уже после сохранённого intent, сначала сравните текущий grant с его digest и зафиксируйте результат штатным аудитом площадки: отсутствие `MUTATION_COMMITTED` не доказывает отсутствие мутации. На целевой платформе эти локальные записи принимает отдельный канонический сборщик 00-INFRA-20261106-0007-workload-and-operator-identity, а конфигурация дополнительно подписывается.

Отзыв одной exact identity:

```sh
python3 provision.py revoke spiffe://example.test/workload/client-a --directory grants \
  --audit-directory audit --actor <operator-subject>
```

Следующий fetch получит 403; уже начатый поток автоматически не прерывается. Для экстренного прерывания используйте утверждённую сетевую изоляцию/остановку конкретного экземпляра. При нескольких репликах примените и подтвердите отзыв на каждой. Не отзывайте весь доверенный CA ради одного клиента, если достаточно точного revoke. Если сам CA скомпрометирован, замените bundle и сертификаты согласованно, затем безопасно пересоздайте TLS listener.

### Автоматическое локальное продление

Для проверки принадлежности уже установленной клиентской задачи используйте `python3.13 -B renewal.py describe --state-directory /operator/private/state`. Успех возвращает в stdout только [закрытую публичную проекцию](SPEC.md#локальное-операторское-продление); команда не продлевает доступ и не исправляет состояние. При `OPERATOR_MUTATION_IN_PROGRESS` дождитесь завершения прежнего цикла и повторите чтение, не останавливая его. Отзыв, expiry, повреждённый аудит и смена scope/CA требуют отдельного разбора, не нового enroll наугад. Параметр указывает прежний операторский каталог; потребитель не читает `approval.json` или аудит самостоятельно и не получает этот каталог в runtime.

Закрепляемый операторский артефакт собирается из этого каталога отдельно: `docker build -f Dockerfile.renewal -t aelev/public-egress-renewal:<release> .`. Он содержит только три модуля и их `renewal.sha256` в `/opt/aelev-egress`, без approvals, ключей, grants или установки таймера. Потребитель проверяет неизменяемый digest образа, marker интерфейса и опись при доставке; сохранённый старый артефакт не перезаписывается. Нативные mutating-команды по-прежнему требуют собственного Python 3.13/OpenSSL и прежних административных полномочий оператора. Сборка этого образа не требует пересоздавать gateway или серверный TLS-таймер.

На локальном стенде оператор после явного согласования может настроить продление уже действующего grant и leaf без передачи административных ключей runtime. Подготовьте закрытый операторский каталог `state` и отдельный публичный каталог отчёта у владельца потребителя; последний не содержит ключей и не даёт права применить grant. Команды выполняются Python 3.13 на операторском хосте с OpenSSL, не внутри gateway или worker:

```sh
python3.13 renewal.py enroll --state-directory /operator/private/state \
  --grant /owner/client/grant.json --issuer /operator/private/issuer \
  --client-directory /owner/client --status-directory /owner/public/egress-status \
  --grants-directory /operator/grants --audit-directory /operator/audit \
  --actor <operator-subject>
python3.13 renewal.py once --state-directory /operator/private/state
python3.13 renewal_schedule.py --state-directory /operator/private/state
```

Серверный leaf одобряется независимо: отзыв любого клиента не должен останавливать обслуживание общего TLS. Укажите отдельный state, стабильный технический идентификатор цели аудита и собственный публичный отчёт:

```sh
python3.13 renewal.py enroll-server --state-directory /operator/private/server-state \
  --issuer /operator/private/issuer --server-directory /operator/gateway-tls \
  --status-directory /owner/public/egress-status --audit-directory /operator/audit \
  --identity spiffe://example.test/workload/gateway-server --actor <operator-subject>
python3.13 renewal.py once --state-directory /operator/private/server-state
python3.13 renewal_schedule.py --state-directory /operator/private/server-state
```

Серверная задача не читает grant или закрытые файлы клиента и пишет `server-status.json`, клиентская — `status.json`. Имена файлов удостоверений: `issuer/ca.pem,ca.key`, `client/client.crt,client.key,grant.json`, `gateway-tls/server.pem,server.key`. Issuer публичный SHA-256 закрепляется, key не копируется; неизменные exact identity/scope и серверные SAN проверяются перед продлением. Каталог одобрения `0700`, файл `0600`; runtime не должен монтировать их. `once --renew-now` для нужного state позволяет проверить реальное продление до порога, но не отменяет ни одной проверки. Сертификат заменяется атомарно с прежними mode и GID. Закрытый ключ не меняется, поэтому нет несовместимой промежуточной пары. Публичная копия grant у клиента не является источником авторизации. Таймер использует закреплённую SHA-256 копию операторских модулей в собственном state, не изменяемое рабочее дерево; для обновления сначала остановите только свой ожидающий таймер, затем установите проверенный пакет снова, сохраняя прежний артефакт.

`renewal_schedule.py` устанавливает только пользовательский LaunchAgent для этого абсолютного state, `RunAtLoad` и интервал 60 секунд, без sudo. Он не работает во время выхода из учётной записи, сна или выключения хоста; запуск после логина/пробуждения проверяет состояние снова. Не используйте эту задачу вместо production SPIRE. На Linux передайте команду `once` действующему операторскому планировщику площадки. Для остановки конкретного macOS таймера получите label из его plist в `~/Library/LaunchAgents/com.aelev.egress-renewal.<hash>.plist` и выполните `launchctl bootout gui/<uid>/<label>`; файл сохраняется до явного удаления. Если нужно запретить будущий автозапуск, переместите только этот plist в архив. Для немедленного прекращения права fetch используйте `provision.py revoke`, не остановку таймера.

Apply/revoke/renewal используют общую операторскую блокировку и проверяемый журнал. После revoke автоматизация не восстанавливает доступ даже при ручном apply; новое одобрение оформляется отдельно после сохранения прежнего state и аудита. `APPROVED_SCOPE_CHANGED`, `APPROVAL_REVOKED`, `GRANT_REVOKED_OR_MISSING`, `EGRESS_GRANT_EXPIRED`, `ISSUER_CHANGED`, `AUDIT_RECONCILIATION_REQUIRED` объясняют закрытый отказ. Не удаляйте аудит и не исправляйте его вручную. При незавершённом intent сначала выполните штатную операторскую сверку. Для отката остановите только таймер, сохраните отчёт/одобрение/аудит и оставьте действующие файлы; не возвращайте просроченные сертификаты или отозванный grant.

Оба отчёта публикуются атомарно: `schema_version=1`, `healthy`, `code`, `stage`, времена `checked_at`, `last_success_at`, `last_renewed_at`, `last_failure_at`, поля последнего сбоя `last_failure_code/stage/type`, `certificate_expires_at`, `grant_expires_at` только у клиента и `ca_expires_at`; времена — UTC Unix seconds. Последний сбой не исчезает после успеха. Наблюдение должно независимо отвергать каждый отчёт старше трёх минут и предупреждать об expiry/отсутствии проверки. Ключей и административных путей в отчётах нет. Ручное изменение expiry останавливает клиентский таймер с `CURRENT_GRANT_CHANGED`, а не отменяется следующим автоматическим продлением. Замена CA не автоматизирована: за десять дней требуется предупреждение, при остатке не более восьми дней выдача нового недельного leaf прекращается с `CA_REPLACEMENT_REQUIRED`. Согласованная смена корней, production SPIRE и внешняя доставка оповещения остаются отдельными проверками площадки.

## 4. Проверить защищённый вызов

Используйте CA и ключи клиента, не серверный ключ. Пример fetch обычного внешнего технического документа:

```sh
curl --cacert /path/to/client/ca.pem \
  --cert /path/to/client/client.pem --key /path/to/client/client.key \
  --header 'Content-Type: application/json' \
  --data '{"method":"GET","url":"https://example.com/","headers":{},"body":"","max_bytes":1048576,"truncate":false}' \
  https://gateway.example.net:18497/v1/fetch
```

Для локального endpoint с выпущенным localhost SAN замените только последний адрес на `https://localhost:18497/v1/fetch`. Ответ с `X-AELEV-Egress-Result: ALLOWED` может иметь внешний 404/429: gateway успешно исполнил сетевую политику, но внешний узел ответил отказом. Для канонического документа клиент проверяет status, длину/хеш и завершение потока; усечённый префикс не является полным документом. Redirect возвращается клиенту и не выполняется автоматически.

Без клиентского сертификата тот же POST возвращает 403. Private/metadata URL блокируется даже у REGULAR; разрешение и локальная PKI не открывают исключение для fixture-сервера. Для проверки health используйте `docker compose exec public-egress-gateway python health.py` — это доверенная собственная TLS-проба, не fetch клиента.

## 5. Подключение контейнера и удалённого узла

На одном Docker host только нужные сетевые роли клиента подключаются к `NETWORK` провайдера как к внешней сети. Его endpoint — `https://public-egress-gateway:8080`; CA/SVID монтируются read-only в поставке владельца. Публичная `PUBLIC_NETWORK` недоступна клиенту. Не добавляйте gateway как контейнер в приложение и не копируйте исходники/образ из соседнего каталога.

На другой машине используется routable HTTPS URL, а не Docker DNS. Платформа клиента разрешает исходящее соединение только к этому endpoint и собственным инфраструктурным зависимостям; Kubernetes — `default-deny` с разрешением точной рабочей нагрузки либо внешнего контролируемого маршрута. Docker host применяет эквивалентный firewall. Не создавайте общую bridge-сеть между странами и не включайте общий direct Web egress для устранения ошибки связи. Без платформенного сетевого запрета закрытый клиентский fallback является необходимой, но недостаточной защитой от компрометации.

## 6. Ошибки и наблюдение

Кнопка «Открыть» в строке gateway ведёт на его собственный Swagger `/docs`; адрес публикует владелец по `DOCS_PUBLIC_URL`, платформа не достраивает ссылку. В локальном примере документация — `https://localhost:18498/docs`, защищённый API — `https://localhost:18497/v1/fetch`; для удалённой машины оператор меняет оба origin и доставляет подходящий серверный сертификат. Группы: `default` — health/readiness, `gateway` — fetch, `metrics` — метрики. Вход документации не запрашивает клиентский ключ и никогда не исполняет POST (405); `Try it out` доступен только техническим GET. Наличие страницы не авторизует fetch на защищённом входе: всё ещё нужны собственные сертификат, URI и действующий grant. На станции без разрешения устанавливать клиентские ключи смотрите схему и выполняйте защищённый пример `curl` выше с разрешённого узла; не используйте ключ издателя или токен оболочки. Рабочий HTTPS-сертификат должен быть доверен браузером без отключения проверки. Маршруты и тестовые границы описаны в [ENDPOINTS.md](ENDPOINTS.md).

| Код | Что проверить |
|---|---|
| `EGRESS_IDENTITY_BLOCKED` | Доверенную цепочку, один URI SAN, exact identity и наличие разрешения. |
| `EGRESS_GRANT_EXPIRED`, `EGRESS_GRANT_INVALID` | UTC-срок, точную форму JSON, числа и scopes; не обходить проверку. |
| `EGRESS_IDENTITY_CONCURRENCY_LIMIT` | Собственную квоту и отложенный повтор с ограниченной задержкой. |
| `EGRESS_CONCURRENCY_LIMIT` | Общую загрузку реплики, суммарные бюджеты; не открывать direct fallback. |
| `EGRESS_IDENTITY_CAPACITY`, `EGRESS_HOST_CAPACITY` | Ограниченные кеши; согласованный rolling restart/бюджеты, не безграничный рост. |
| `EGRESS_HOST_BLOCKED`, `EGRESS_ADDRESS_NOT_GLOBAL`, `EGRESS_ADDRESS_DENIED` | Реальное назначение, весь DNS-ответ, универсальные и дополнительные запреты. |
| `EGRESS_METHOD_BLOCKED`, `EGRESS_POST_SCOPE_BLOCKED` | Режим identity и exact host/path. |
| `EGRESS_CREDENTIAL_BLOCKED`, `EGRESS_CREDENTIAL_SCOPE_BLOCKED`, `EGRESS_CREDENTIAL_REQUIRES_TLS` | Точный header/host и HTTPS; credentials остаются у владельца. |
| `EGRESS_LIMIT_INVALID`, `EGRESS_RESPONSE_TOO_LARGE` | Индивидуальный предел, предел запроса и полноту потока. |
| `EGRESS_DNS_NOT_FOUND`, `EGRESS_DNS_TEMPORARY`, `EGRESS_DNS_UNAVAILABLE` | Отличить отсутствующее имя от временной ошибки; решение повтора принимает владелец. |
| `EGRESS_UPSTREAM_FAILED`, `EGRESS_UPSTREAM_TIMEOUT` | Внешний endpoint, route и timeout; сообщения исключений/секреты не журналируются. |

`docker compose logs public-egress-gateway` показывает безопасный `request_id`, hostname, метод, результат, байты и длительность, не полный URL/secret. `/metrics` содержит только технические счётчики процесса. В local можно оставить `OTLP_HTTP_ENDPOINT` пустым. Для production доставьте две отдельные mTLS-пары в `secrets/telemetry/client` и `secrets/telemetry/sender`, задайте `OTLP_HTTP_ENDPOINT=https://public-egress-gateway-telemetry-sender:4318`, один раз подготовьте том командой `docker compose --profile provision run --rm public-egress-gateway-telemetry-data-init`, затем запустите `docker compose --profile telemetry up -d`. Runtime видит только client identity, sender — только свои listener/central-delivery identities; постоянный volume сохраняет журнал при недоступности центральной наблюдаемости. Публикация состояния и тесты описаны в [README](README.md).

## Официальные руководства

- [FastAPI deployment concepts](https://fastapi.tiangolo.com/deployment/concepts/) — запуск защищённого API, workers и завершение процесса.
- [HTTPX advanced clients](https://www.python-httpx.org/advanced/clients/) и [timeouts](https://www.python-httpx.org/advanced/timeouts/) — connection pooling и раздельные сетевые пределы.
- [Python ipaddress](https://docs.python.org/3.13/library/ipaddress.html) — классификация разрешённых и запрещённых адресов после DNS resolution.
- [OpenTelemetry OTLP](https://opentelemetry.io/docs/specs/otlp/) — доставка logs/traces/metrics, backpressure и повтор.
- [SPIFFE X.509-SVID](https://spiffe.io/docs/latest/spiffe-specs/x509-svid/) — workload identity для mTLS.
