# Ячейка внешнего провайдера перевода

## Независимое сетевое размещение

По [00-INFRA-0018](../../docs/adr/00-INFRA-20261113-0018-independent-roles-and-translation-mtls.md) `--role provider` и `--role worker` выбирают разные самостоятельные recipes. Каждая установка управляет только локальным Docker daemon своего узла; связь между ролями проходит по явному HTTPS/mTLS origin и не использует общий Docker volume. `--role bundle` по умолчанию сохраняет прежний совместный UDS recipe ниже и не является распределённой приёмкой.

Подготовьте отдельный закрытый env на каждом целевом узле по [provider example](compute.provider.env.example) или [worker example](compute.worker.env.example). Шаблоны закрепляют exact network images после [main worker artifact qualification](../../docs/audit/translator-psycopg-main-20261114.json); эта ограниченная проверка не заменяет owner queue/quality, подготовку площадки и controlled rollout. Адреса, credentials и SVID volumes задаются для своих узлов. Имя Compose project уникально для placement/replica. Каждый экземпляр получает собственный SVID volume и ключ; другой узел не должен монтировать этот же файловый volume. Публичная identity policy связывает разрешённые provider/worker workload identities одного trust domain, но не содержит private keys или API credential.

Provider получает только собственную SVID delivery, read-only identity policy, generic `transport.json` и отдельный credential file. Listener configuration содержит ровно `schema_version=1`, `transport=mtls`, `listen_host=0.0.0.0`, `listen_port=18487`. Recipe публикует этот listener на явно выбранном адресе/порте своего узла. DNS/SAN должны соответствовать origin worker. Provider command, environment, entrypoint и image healthcheck не переопределяются.

Worker получает собственную SVID delivery, public identity policy, свои PostgreSQL/telemetry credentials и маршрутизируемые endpoints. API credential provider ему недоступен. Identity policy содержит ровно `schema_version=1`, `environment`, `delivery`, `server_spiffe_id`, `client_spiffe_id`; URI принадлежат `/translator-service/` одного trust domain. `environment` совпадает с env worker, production требует актуальный `spire-file-v1`. Local/test могут явно использовать `pem-files-v1` в собственной подготовленной SVID volume; это не production PKI. TLS-проверки, свежесть, exact URI, CA и hostname не отключаются. `GET /healthz` не вызывает OpenAI и не доказывает quota/качество.

На узле provider выполните:

```sh
sh compute-runtime/bootstrap.sh doctor --role provider --env-file /operator/provider.env
sh compute-runtime/bootstrap.sh prepare --role provider --env-file /operator/provider.env
sh compute-runtime/bootstrap.sh start --role provider --env-file /operator/provider.env
```

На узле worker выполните:

```sh
sh compute-runtime/bootstrap.sh doctor --role worker --env-file /operator/worker.env
sh compute-runtime/bootstrap.sh prepare --role worker --env-file /operator/worker.env
sh compute-runtime/bootstrap.sh start --role worker --env-file /operator/worker.env
```

`status` и `stop` принимают тот же явный `--role` и env своего placement. Windows entry передаёт те же arguments в `station.py`. Новая роль не запускает вторую; существующий runtime не переключается из bundle автоматически. `start` отклоняет чужой project/working directory, лишние replicas в одном placement, остановленную цель, изменённый image/config и unhealthy incarnation. Нужен отдельный владельческий handoff с сохранением истории; команда не сбрасывает restart budget и не выдаёт `RESUME`. Следующая replica устанавливается отдельным placement с собственными keys, а не масштабированием общего SVID mount.

Это интерфейс установки компонентов, не завершённый многоузловой координатор. Полная автоматизация inventory, новые signed releases, exact provider/worker integration, оба полных clean-room режима и разные физические узлы допускаются отдельно. Ни `doctor`, ни успешный transport test не обозначают полную production readiness.

Отдельная воспроизводимая проверка сетевых images — `test/network_provider_bundle.py` из каталога владельца. Она принимает immutable `--provider-image`, `--worker-image`, два проверенных непересекающихся RFC1918 `/28` через `--provider-subnet`/`--worker-subnet` и `--report` вне checkout. Используются два разных Docker network, опубликованный endpoint, раздельные новые TLS volumes и временная test PKI; действительный same-CA foreign client отклоняется, положительный probe повторяется после отказа. OpenAI не вызывается. Проверяются non-root первый mount и cleanup только собственных временных ресурсов. Для локальной разработки `--allow-local-image-ids` разрешает только immutable `sha256:...`, явно без release admission; mutable tags не принимаются. Этот тест не закрывает физическую многоузловую приёмку.

## Совместимый UDS bundle

> Этот recipe размещает выбранный непрозрачный provider image рядом с worker.
> Root/full-local и release BOM используют тот же recipe с exact image digests;
> это не раскрывает реализацию и само по себе не является production admission.

Этот каталог соединяет внешний provider image и принадлежащий Translator worker
через `doctor`, `prepare`, `start`, `status` и `stop`. Реализация провайдера в
репозиторий не входит: `translator-service` передаёт ему только текст и языки и
получает строку перевода либо ограниченную ошибку. Compose не переопределяет
`ENTRYPOINT`, `CMD`, переменные или `HEALTHCHECK` provider image. Он предоставляет
только исходящую сеть без опубликованных портов, UDS volume и generic read-only
secret file `/run/secrets/provider/credential.env`. Модель, prompt, endpoint и способ
исполнения остаются у поставщика провайдера. Каталог не запускает Kafka
consumers, outbox или UI и не управляет удалённым Docker daemon.

Текущий шаблон закрепляет [provider v0.1.1](https://github.com/sapogov1978/AELEV-translation-provider/releases/tag/v0.1.1) и прежний совместимый worker. Component evidence содержит проверки подписи, обеих native-архитектур, ограниченного security-профиля и отдельный live quality run. Штатные read-only root, non-root UID образа, dropped capabilities и no-new-privileges обязательны для этого профиля; собственный worker не получает implementation settings провайдера.

Скопируйте [compute.env.example](compute.env.example) в закрытый файл станции,
задайте точные образы `name@sha256:...`, маршруты собственной PostgreSQL/Telemetry
Delivery, их external network flags и пути к identities. В корневом локальном
стеке эти две сети принадлежат Translator и создаются самим Compose; отдельная
compute-станция подключается к уже маршрутизируемым owner networks. Соседний
игнорируемый
`TranslationProvider/.env.local` с правами `0600` служит источником для
create-only импорта в `translator-service/runtime/provider-credential.env`;
вручную задайте в нём `OPENAI_API_KEY` и не передавайте значение через Git,
журнал или командную строку. На macOS/Linux используйте:

```sh
sh compute-runtime/bootstrap.sh doctor --env-file compute-runtime/compute.env
sh compute-runtime/bootstrap.sh prepare --env-file compute-runtime/compute.env
sh compute-runtime/bootstrap.sh start --env-file compute-runtime/compute.env
sh compute-runtime/bootstrap.sh status --env-file compute-runtime/compute.env
sh compute-runtime/bootstrap.sh stop --env-file compute-runtime/compute.env
```

На Windows используйте те же действия через `compute-runtime\bootstrap.ps1`. Требуются Python 3.13, локальный Docker в режиме Linux containers и совместимые с архитектурой станции допущенные образы. Provider image обязан сам объявить рабочий `HEALTHCHECK` и слушать `/run/aelev-translator/translator.sock`; без этого `start --wait` закрыто завершается ошибкой. Успешный `doctor` проверяет только структуру поставки и digests, а не качество перевода или внутренности провайдера.
