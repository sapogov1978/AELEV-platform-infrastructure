# HTTP endpoints Public Egress Gateway

Этот документ описывает endpoints самостоятельного Public Egress Gateway. Swagger доступен на read-only documentation listener по `/docs`, схема — по `/openapi.json`; фактический `POST /v1/fetch` выполняется через защищённый gateway listener, указанный в OpenAPI. Канонические ограничения назначения и прав определены [SPEC](SPEC.md), [USER_MANUAL](USER_MANUAL.md) и [ADR-0114](../docs/adr/20261012-0114-infrastructure-public-egress-gateway.md).

## Доступ и общие правила

Documentation listener обслуживает только Swagger/OpenAPI и не проксирует запросы к внешней сети. `POST /v1/fetch` требует доверенный клиентский сертификат с одной точной SPIFFE URI и действующую индивидуальную декларацию прав. Наличие сертификата без grant не разрешает запрос. `/healthz`, `/readyz` и `/metrics` не раскрывают grants, credentials или содержимое документов.

Gateway не следует redirect, блокирует internal, special-use, metadata и DNS-rebinding назначения, закрепляет проверенный IP и ограничивает запрос и ответ. `X-AELEV-Egress-Result: ALLOWED` означает прохождение политики gateway, а не успешность upstream. Для отказов gateway использует `X-AELEV-Egress-Result: REJECTED` и JSON `{"status":"rejected","code":"..."}`.

## Endpoints, показанные в Swagger

| Метод и путь | Доступ | Вход | Назначение и ответ |
|---|---|---|---|
| `GET /healthz` | без клиентского удостоверения | — | Проверяет только живость процесса. Возвращает `200 application/json` и `{"status":"ok"}`; зависимости и grants не проверяются. |
| `GET /readyz` | без клиентского удостоверения | — | Проверяет готовность gateway. Возвращает `200 application/json` и `{"status":"ready"}`; пустой каталог grants считается штатным deny-by-default и не подтверждает доступность внешних upstream. |
| `POST /v1/fetch` | mTLS workload identity и действующий exact grant | JSON из шести обязательных полей: `method`, `url`, `headers`, `body`, `max_bytes`, `truncate` | Выполняет один ограниченный внешний `GET`, `HEAD` или разрешённый `POST` и потоково возвращает нативный upstream status, безопасные headers и байты. Redirect не выполняется. |
| `GET /metrics` | техническая сеть, без клиентского удостоверения | — | Возвращает Prometheus-метрики процесса без grants, credentials и тел документов. |

## Тело `POST /v1/fetch`

| Поле | Ограничение |
|---|---|
| `method` | Только `GET`, `HEAD` или `POST`. |
| `url` | Публичный HTTP(S) URL до 8192 символов, порты 80/443, без userinfo и fragment; каждый DNS-адрес проверяется. |
| `headers` | Объект только из закрытого безопасного списка runtime. `Authorization` и `X-Subscription-Token` допустимы лишь для exact REGULAR HTTPS scope; `Cookie`, CR/LF/NUL запрещены. |
| `body` | Base64, после декодирования не более 65536 байт. Для `GET`/`HEAD` пусто; `POST` требует exact разрешённый HTTPS host/path и допустимый `application/json`. |
| `max_bytes` | Целое от 1 до настроенного server maximum и не выше индивидуального grant; boolean не принимается как число. |
| `truncate` | `true` явно разрешает только ограниченный prefix, но не доказывает полноту; запрещено для credentialed-запросов. Полные документы и Range-загрузки используют `false`. |

Успешно допущенный ответ содержит `X-AELEV-Egress-Result: ALLOWED` и `X-AELEV-Egress-Request-ID`; ограниченный prefix дополнительно получает `X-AELEV-Response-Truncated: bounded`, а известная upstream длина для `HEAD` — `X-AELEV-Upstream-Content-Length`. После отправки headers превышение предела или upstream failure обрывает поток: неполные байты нельзя считать полным документом. Gateway возвращает собственные отказы `400`, `403`, `404`, `429`, `502` или `504` в зависимости от причины.

## Служебные endpoints Swagger

| Метод и путь | Назначение |
|---|---|
| `GET /docs` и `GET /docs/` | Read-only Swagger UI на отдельном documentation listener. |
| `GET /openapi.json` | Схема OpenAPI, построенная из того же закрытого wire-контракта, что и parser runtime. |
| `GET /docs/assets/*` | Закреплённые локальные ресурсы Swagger UI без CDN. |

Любой `POST` на documentation listener возвращает `405` и не становится обходом mTLS gateway listener.
