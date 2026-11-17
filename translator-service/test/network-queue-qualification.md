# Изолированная сетевая очередь Translator

`network_queue_lifecycle.py` создаёт собственные Kafka/PostgreSQL, consumer, worker, provider и outbox. Скрипт не принимает адрес существующей БД/очереди. Рабочие Compose projects, очереди и Process Control не выбираются. Нужны Docker, OpenSSL и Python 3.13 для orchestration; продукт работает внутри native images.

Первый режим использует `network_queue_fixture.py`: это контролируемый HTTPS порт с фиксированным ответом и счётчиком, без ИИ. Он проверяет доставку и восстановление, а не качество перевода. `network_queue_probe.py` запускается внутри core image и пользуется каноническими contracts/runtime/repository.

```sh
python translator-service/test/network_queue_lifecycle.py \
  --core-image IMMUTABLE_CORE_IMAGE \
  --worker-image IMMUTABLE_WORKER_IMAGE \
  --database-image IMMUTABLE_POSTGRESQL_IMAGE \
  --kafka-image IMMUTABLE_KAFKA_IMAGE \
  --output PRIVATE_EVIDENCE_ROOT
```

Все images должны быть заранее доступны и заданы через `@sha256:…` либо полный локальный `sha256:…` ID. Core и worker проверяются на одинаковые runtime source hashes, Python 3.13.7, SQLite 3.53.4 и Psycopg 3.3.9. Core собирается штатным target `translator-core-runtime`; его локальная сборка не означает release admission API/consumer/outbox images. В qualification применяются прежние PostgreSQL 17.6 и Kafka 3.9.1; конкретные image IDs записываются в receipt.

По умолчанию запрос публикуется в `translation.requests.v2`; `--priority` выбирает `translation.requests.priority.v2`. Оба канонических consumer работают с разными identities и ACL. Queue selection явно записывается в receipt; одна обработанная очередь не выдаётся за проверку другой. Оснастка и images получают exact hashes; изменение test sources во время прогона отклоняет PASS.

У каждой роли свой TLS secret volume. PostgreSQL использует `verify-full`, password и client certificate; Kafka требует mTLS и явные runtime ACL. Клиенты обращаются к опубликованным адресам через разные bridge networks. Worker и provider не используют общий том. Временная test PKI и один Docker Engine не доказывают production identity admission или разные физические узлы.

Проверяется настоящая обработка v2 CloudEvent с одним checkpoint segment. После первой compute lease тест посылает `PAUSE` только своему новому store; `RESUME` не выполняется. Первый outbox publisher аварийно завершается с кодом 73 непосредственно после успешного delivery callback Kafka, до записи `PUBLISHED`. Обычный outbox публикует прежние bytes повторно. Точный повтор входного CloudEvent снова выдаёт те же result bytes без нового вычисления. Итог: одна compute attempt, три wire result records с одинаковым hash.

Семантика повторов соответствует [Kafka delivery semantics](https://kafka.apache.org/39/design/design/#message-delivery-semantics): broker idempotence сама по себе не делает атомарными Kafka ACK и локальный SQL outbox state. Подлинность узлов проверяется согласно [Kafka SSL](https://kafka.apache.org/39/security/encryption-and-authentication-using-ssl/) и [PostgreSQL verify-full](https://www.postgresql.org/docs/17/libpq-ssl.html). Проверка `pg_stat_ssl.client_dn` использует `/CN=queue-owner`: именно такой slash-separated DN выдаёт [PostgreSQL 17.6 `X509_NAME_to_cstring`](https://github.com/postgres/postgres/blob/REL_17_6/src/backend/libpq/be-secure-openssl.c#L1557). В [confluent-kafka 2.11.1](https://github.com/confluentinc/confluent-kafka-python/blob/v2.11.1/src/confluent_kafka/admin/_resource.py) ACL resource type 4 называется `BROKER`; для `IDEMPOTENT_WRITE` используется именно этот enum и стандартное имя `kafka-cluster`.

Для отдельной проверки выпущенного opaque provider добавляются:

```sh
  --live --provider-image IMMUTABLE_PROVIDER_IMAGE \
  --provider-credential EXISTING_PRIVATE_CREDENTIAL_FILE \
  --request PRIVATE_TRANSLATION_REQUEST_JSON
```

Request JSON содержит только `source_language`, `target_language`, `text`. Целое поле текста должно занимать 1–800 символов и одну строку: сценарий не обрезает материал и допускает один checkpoint segment. Provider запускается со своими image command/environment/healthcheck; credential монтируется read-only только в provider. До запуска live оператор должен иметь разрешение владельца на этот ограниченный запрос и использование существующего ключа.

Языковые коды задаёт вызывающий владелец по inventory выбранного provider. Оснастка не угадывает их и не нормализует вместо provider; например, для выпущенного translation provider v0.2.1 используются `en` и `ru`. Отказ несовместимого кода остаётся неудачным live-input case, а не доказательством перевода или недостатка API quota.

Результат `PASS` — технический queue proof. Для live отдельно проверяется смысл фактического перевода и сохраняется ссылка на исходный материал. Полная текущая политика, каскад Discovery и deployment admission этим не подтверждаются.

Внешний `--output` содержит приватные raw request/result bytes, command logs, временную PKI и verification JSON. Эти данные не коммитятся и не публикуются автоматически. Cleanup удаляет только объекты со своим случайным scope label, включая собственные SQL/Kafka data volumes, и сверяет сохранность исходных постоянных контейнеров. Неудачный прогон сохраняет `FAIL`, а не новый admission.
