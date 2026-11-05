# MinIO — руководство пользователя

## Структура поставки

```text
minio/
├── README.md, SPEC.md, TASKS.md, CHANGELOG.md
├── compose.yml                   # две площадки, издатель, явные provision
├── .env.example                  # шаблон; .env — рабочие настройки вне Git
├── Dockerfile                    # закреплённые MinIO и mc
├── Dockerfile.status
├── generate-local-configuration.sh  # только первичная локальная подготовка
├── configure-site-replication.sh
├── provision-bucket.sh           # универсальная операция, без списка клиентов
├── healthcheck.sh
├── pyproject.toml, poetry.lock
├── status_publisher.py, vendor/
├── secrets/
│   ├── README.md
│   ├── root-user, root-password  # отдельная доставка, вне Git/образа
│   ├── sites-ca.crt
│   ├── site-a/                   # public.crt, private.key площадки A
│   ├── site-b/                   # public.crt, private.key площадки B
│   ├── status/                   # издатель состояния
│   └── status-admin/             # только одноразовый provision
└── test/                         # проверки и читаемые результаты
```

Дерево показывает значимые файлы поставки, а не кэши инструментов. `.env`, содержимое `secrets/` и производные `runtime/` создаются оператором и не входят в Git или образ; документация `secrets/README.md`, где она есть, остаётся в Git. Постоянные данные находятся в именованных Docker volumes из Compose, не в каталоге исходников.

## Назначение


MinIO предоставляет самостоятельно размещаемое S3-совместимое объектное хранилище для крупных неизменяемых байтов. PostgreSQL остаётся владельцем транзакционного состояния, манифестов, хешей и контрольных точек, Kafka доставляет события, а MinIO хранит сами объекты, которые не следует помещать в строки базы или локальный каталог контейнера. Компонент не знает имён потребителей, bucket или прикладных политик.

Собственный издатель имеет `stop_grace_period: 2m`: это время на завершение текущего запроса и подтверждение отписки через удалённый HTTPS, не запасное значение env и не бесконечное ожидание. Если связь недоступна, неподтверждённый выход сохраняется в журнале, а UI не скрывает строку как штатно завершённую.

В «Состоянии» верхняя строка называется «Хранилище артефактов» (`Artifact storage`) и отражает назначение, а раскрываемый состав показывает технологии и процессы этой поставки. Названия публикует сам владелец в ревизии 2; оболочка не содержит таблицы переименований. UUID, identity, состав и последовательность сохраняются.

Обязателен не продукт MinIO как таковой, а S3-совместимый способ надёжно хранить крупные объекты отдельно от транзакционной базы. Если площадка уже предоставляет утверждённое S3-совместимое хранилище с сопоставимыми гарантиями, этот компонент не запускается, а потребители получают адрес и учётные данные поставщика. При самостоятельном размещении DRP эту возможность реализует закреплённый MinIO.

## Секреты: получение и подготовка

| Файл | Источник и назначение |
|---|---|
| `secrets/root-user`, `root-password` | Уникальные корневые access/secret keys установки. Для локального стенда их создаёт собственный генератор, для рабочей среды выдаёт внешнее хранилище. Не выдаются обычным потребителям. |
| `secrets/site-a/public.crt`, `private.key` | Удостоверение площадки A: PEM-сертификат с цепочкой и соответствующий ключ. SAN покрывает используемые адреса узлов и входа площадки. |
| `secrets/site-b/public.crt`, `private.key` | Отдельная пара площадки B с её адресами; не копия ключа другой площадки. |
| `secrets/site-{a,b}/CAs/aelev-minio-sites.crt` | Публичная owner-копия `sites-ca.crt` внутри каждого TLS-каталога. Узел монтирует каталог целиком, поэтому атомарная замена leaf не остаётся на старом inode. |
| `secrets/sites-ca.crt` | Проверенные публичные цепочки доверия обеих площадок. Включает обе локальные self-signed цепочки в стенде; закрытых CA-ключей нет. |
| Реквизиты создаваемого владельца bucket | Выдаёт хранилище секретов конкретного владельца. Универсальная `bucket-provisioner` получает их только на время точной операции; инфраструктура не хранит список клиентов. |

Для новой локальной установки выполните `./generate-local-configuration.sh` из этого каталога: он создаёт env, случайные ключи, две self-signed TLS-пары и набор доверия, не печатая секреты и отказываясь перезаписывать существующее. Сертификаты действуют 365 дней; генератор не обновляет их автоматически. Это не доверие браузера и не замена рабочей PKI. Ограничения самого локального генератора отмечены в [TASKS](TASKS.md).

Для рабочей пары площадок ключ и CSR создаются на станции оператора отдельно для каждой площадки. Пример только для нового ключа площадки A; реальные имена замените своими, для B повторите со своим каталогом и адресами:

```sh
umask 077
install -d -m 700 secrets/site-a
test ! -e secrets/site-a/private.key || exit 1
openssl req -new -newkey rsa:3072 -nodes -sha256 \
  -subj /CN=minio-site-a \
  -addext 'subjectAltName=DNS:s3-a.example.net,DNS:storage-a-1.example.net,DNS:storage-a-2.example.net,DNS:storage-a-3.example.net,DNS:storage-a-4.example.net' \
  -addext 'extendedKeyUsage=serverAuth' \
  -keyout secrets/site-a/private.key -out secrets/site-a/server.csr
```

CA возвращает цепочку `public.crt`; доверенные цепочки обеих площадок собирает оператор в `sites-ca.crt`. CSR не содержит закрытого ключа. Проверяйте SAN и срок `openssl x509 -in secrets/site-a/public.crt -noout -dates -ext subjectAltName`; на площадку доставляют только её ключ/сертификат и общее публичное доверие. Текущий Compose использует одну пару на четыре узла площадки: это фактическая область действия данного ключа, не индивидуальная SVID каждого узла.

Root-файлы и private.key имеют `0600`, сертификаты `0644`; native-процессы текущего образа читают их через read-only mounts. Издатель не получает root keys, его probe-ca содержит только публичное доверие. Смена корневых реквизитов согласуется между обеими площадками и административными задачами с проверкой site replication; простой повтор генератора или изменение одного файла не является ротацией. Потребительские ключи меняет их владелец через собственную выдачу прав. TLS обновляют адресно с сохранением кворума каждой площадки и проверкой репликации, не одновременной остановкой всех восьми узлов.

### Секреты издателя состояния

Пути ниже соответствуют шаблону `UI_SECRET_ROOT=./secrets/status`; при другом значении используйте собственный настроенный каталог. Источники доверия определяются адресами, не совместным размещением: узел в другой стране получает публичные цепочки CA и своё удостоверение через защищённую доставку, а не доступ к чужой папке.

| Файл | Откуда получить и зачем |
|---|---|
| `shell-ca.crt` | Проверенная публичная цепочка CA HTTPS-сервера оболочки; получить у её оператора. Это не CA клиентских издателей. |
| `identity-ca.crt` | Проверенная публичная цепочка CA HTTPS IdP; получить у оператора IdP. |
| `publisher.crt`, `publisher.key` | Собственное удостоверение `clientAuth`; его выпускающая CA должна входить в `publisher-ca.crt` приёмника UI. Закрытый ключ принадлежит только этому издателю. |
| `client-secret` | Выдаёт Keycloak для собственного confidential client; сохраняет `minio-status-provision`. Не генерируйте произвольный пароль вместо секрета существующего OIDC-клиента. |
| `probe-ca.crt` | Публичный набор доверия MinIO обеих площадок; для локального стенда содержимое собственного `sites-ca.crt`. Read-only HTTPS-проба не получает root credentials. |
`UI_ADMIN_ROOT` содержит отдельные `admin-user` и `admin-password`: имя и действующий пароль администратора развёртывания Keycloak получают у оператора IdP или из внешнего хранилища. Это не пароль service account и не пароль пользователя браузера. Доставьте их файловым механизмом; например, `install -m 640 /secure/idp-admin-password secrets/status-admin/admin-password`, а имя запишите в `admin-user` без секрета. Provision должен иметь право записи в `UI_SECRET_ROOT`, runtime — только чтение.

Перед локальной подписью проверьте `openssl version` и поддержку OpenSSL 3; при отсутствии используйте утверждённый пакет вашей ОС (`brew install openssl@3` на macOS, `sudo apt-get install openssl` на поддерживаемой Debian/Ubuntu) или подготовленную операторскую станцию. Эти команды не устанавливают CA в системное доверие и не выполняются при запуске контейнера.

Первичная доставка административных файлов (пути источника и имя замените данными своего IdP, не пароль в команде):

```sh
umask 077
install -d -m 750 secrets/status-admin
test ! -e secrets/status-admin/admin-user && test ! -e secrets/status-admin/admin-password || exit 1
STATUS_ADMIN_USER=deployment-admin
printf '%s\n' "$STATUS_ADMIN_USER" > secrets/status-admin/admin-user
install -m 640 /secure/idp-admin-password secrets/status-admin/admin-password
chmod 640 secrets/status-admin/admin-user
```

Имя deployment-admin — пример существующей identity, не команда создания пользователя. Если у вас другое имя или UI_ADMIN_ROOT, задайте их явно; GID каталога/файлов должен совпадать с UI_SECRET_GID, а владелец — с UI_PROVISION_UID. Уже доставленные пароли не перезаписываются первичной командой. После удаления одноразового контейнера пароль не нужен рабочему издателю; локальную административную доставку уберите из runtime-узла или выдавайте заново только на время provision.


Для новой локальной identity можно создать ключ и CSR на доверенной операторской машине. Нужен OpenSSL 3 для `-copy_extensions`; пути `/secure/...` ниже заменяются выбранными операторскими файлами, это не обязательные каталоги сервиса. Публичные CA берутся из проверенного источника; неизвестный сертификат из сети не становится доверенным только потому, что назван `ca.crt`.

```sh
umask 077
install -d -m 750 secrets/status
test ! -e secrets/status/publisher.key && test ! -e secrets/status/publisher.crt || exit 1
openssl req -new -newkey rsa:3072 -nodes -sha256 \
  -subj /CN=publisher -addext 'extendedKeyUsage=clientAuth' \
  -keyout secrets/status/publisher.key -out secrets/status/publisher.csr
# Только локальный тест: ключ CA остаётся на операторской машине, не в runtime.
openssl x509 -req -sha256 -days 30 -copy_extensions copy \
  -in secrets/status/publisher.csr \
  -CA /secure/ui-publisher-ca.crt -CAkey /secure/ui-publisher-ca.key \
  -CAcreateserial -out secrets/status/publisher.crt
install -m 640 /secure/ui-server-ca.crt secrets/status/shell-ca.crt
install -m 640 /secure/idp-ca.crt secrets/status/identity-ca.crt
chmod 640 secrets/status/publisher.key secrets/status/publisher.crt
```

Тестовую CA издателей создаёт оператор локальной оболочки при её явной подготовке PKI; берутся именно `issuer/publisher.crt/key`, не серверный ключ UI. В рабочей среде последнее подписание локальной CA не выполняют: удостоверение и обновляемую identity доставляет утверждённая PKI/SPIRE по [ADR-0017](../../../docs/adr/20260803-0017-zero-trust-and-operational-security.md). CSR можно передать CA, закрытый ключ нельзя. Перед включением издателя доставьте также перечисленные файлы нативных проб и выполните собственный `--profile provision run --rm minio-status-provision` из раздела запуска.


Для локальной пробы после собственного генератора достаточно доставить публичное доверие: `install -d -m 750 secrets/status`, затем `install -m 640 secrets/sites-ca.crt secrets/status/probe-ca.crt`. Приватный MinIO key и root-password издателю не передаются.


### Хранение, доступ и ротация

Секреты разделяются по среде, владельцу и роли. Локальные файлы — только материал стенда или доставка, не каноническое production-хранилище; рабочий источник — внешнее утверждённое хранилище, доставка в память/tmpfs по ADR-0017. Не включайте секреты в Git, образ, архив исходников, команды с подстановкой содержимого или отчёты. Не запускайте `set -x`, не публикуйте раскрытый `docker compose config`; проверяйте через `config --quiet`. Резервные копии секретов хранятся отдельно, зашифрованно, с ограниченным доступом; закрытые CA — только у оператора/KMS/HSM, никогда у рабочего процесса.

Для status-файлов установите каталоги `0750`, файлы `0640`, группу из собственного `UI_SECRET_GID`; владельцем каталога должен быть оператор с `UI_PROVISION_UID`, которому разрешена запись client-secret. UID рабочего издателя остаётся заданным собственным образом/Compose; дополнительная группа даёт только чтение. В командном примере ниже STATUS_SECRET_GID — числовое значение уже настроенного UI_SECRET_GID, не новая переменная сервисного env. Не делайте ключи общедоступными ради запуска. Права нативных секретов описаны отдельно выше.

```sh
: "${STATUS_SECRET_GID:?set the numeric GID from your UI_SECRET_GID}"
chgrp "$STATUS_SECRET_GID" secrets/status secrets/status/*
chmod 750 secrets/status
chmod 640 secrets/status/*
```

Для отдельного административного каталога примените ту же группу, `0750` каталогу и `0640` обоим файлам; владельцу из UI_PROVISION_UID должно быть разрешено чтение admin-password и запись client-secret в status. Keycloak использует административный пароль через env, а не status-admin.

Проверьте срок, SAN/EKU и цепочку сертификата до запуска: `openssl x509 -in secrets/status/publisher.crt -noout -dates -ext extendedKeyUsage`, `openssl verify -CAfile /secure/ui-publisher-ca.crt -purpose sslclient secrets/status/publisher.crt`. Локальное удостоверение из команды действует 30 дней и не обновляется автоматически. Для смены CA сначала доставляют доверие старой и новой цепочкам, затем новые удостоверения, проверяют соединения и только после этого отзывают старые. Ротацию проводят через отдельный подготовленный набор файлов и контролируемое пересоздание собственной роли; генераторы первичного запуска не используют для перезаписи активных ключей. Новый client-secret сначала создают в собственном клиенте Keycloak, затем доставляют повторным собственным provision и пересоздают издателя; обычный повтор provision сам по себе не является ротацией. UUID, identity, ревизия и журнал последовательностей при этом сохраняются.


## Почему это два кластера, а не один растянутый кластер

Рабочая поставка состоит из двух независимых географических площадок. На каждой площадке работают четыре равноправных узла MinIO с отдельными локальными томами и erasure coding `EC:2`; между площадками включается MinIO site replication. Один кластер не растягивается через WAN: его операции требуют кворума и поэтому должны оставаться внутри площадки с предсказуемой задержкой, тогда как географическую потерю площадки покрывает репликация между самостоятельными кластерами.

На каждой площадке потеря одного узла сохраняет чтение и запись. При потере двух из четырёх томов чтение ещё возможно, но запись блокируется, потому что `EC:2` требует три доступных тома для write quorum. Потеря всей площадки не уничтожает уже реплицированные объекты второй площадки. Репликация между площадками асинхронна: её отставание должно наблюдаться, измеренный допустимый RPO утверждается площадкой, а отдельная неизменяемая резервная копия всё равно обязательна, потому что репликация не защищает от логического удаления, скомпрометированных полномочий или ошибки оператора.

MinIO рекомендует для рабочей распределённой поставки не менее четырёх однородных узлов на площадку, локально подключённые диски и балансировщик перед каждым кластером. Site replication предназначена именно для независимых центров обработки данных и географических регионов. Основания: [архитектура развёртывания](https://min.io/docs/minio/linux/operations/concepts/architecture.html), [многоузловая поставка](https://min.io/docs/minio/linux/operations/install-deploy-manage/deploy-minio-multi-node-multi-drive.html), [erasure coding](https://min.io/docs/minio/linux/operations/concepts/erasure-coding.html) и [site replication](https://min.io/docs/minio/linux/operations/install-deploy-manage/multi-site-replication.html).

## Локальная подготовка и запуск

Все команды без другого указания выполняются из этого каталога, в том числе после извлечения поставки из монорепозитория. Если работает общая группа инфраструктуры, управляйте MinIO через [её Compose](../README.md), не создавайте вторую группу писателей тех же томов. Генератор предназначен только для новой локальной установки: при наличии `.env` или собственных ключей он отказывает, не обновляет и не перезаписывает их. Для уже подготовленной установки пропустите генерацию и сборку неизменившегося образа.

Локальный стенд воспроизводит полную логическую топологию из восьми контейнеров на одной машине. Он проверяет кворум, TLS и site replication, но одна машина не является восемью областями отказа и не доказывает географическую отказоустойчивость.

Готовый registry-образ закреплённого выпуска больше недоступен, поэтому [`Dockerfile`](Dockerfile) воспроизводимо собирает сторонние MinIO и `mc` из точных официальных commit. Это не добавляет Go в исходный стек DRP: в каталоге нет проектных `.go`-файлов, компилятор существует только во временном builder-слое, а рабочий слой содержит готовые бинарники, лицензии и штатный shell healthcheck через `mc ready`. Версии Go builder и UBI runtime закреплены по digest, исходники MinIO и `mc` — по полным commit, а тест сверяет встроенные release и commit обоих бинарников перед запуском узлов.

```sh
docker version
docker compose version
./generate-local-configuration.sh
docker compose --env-file .env --profile provision config --quiet
docker compose --env-file .env build minio-site-a-1
docker compose --env-file .env up -d --wait
docker compose --env-file .env --profile provision run --rm site-replication-applier
```

Подготовка bucket выполняется после site replication отдельной одноразовой задачей владельца ресурса. Обычный запуск узлов не знает имён bucket и не запускает эту задачу.

```sh
: "${AELEV_INFRASTRUCTURE_MINIO_PROVISION_BUCKET:?load the owned bucket name}"
: "${AELEV_INFRASTRUCTURE_MINIO_PROVISION_ACCESS_KEY:?load from the approved secret store}"
: "${AELEV_INFRASTRUCTURE_MINIO_PROVISION_SECRET_KEY:?load from the approved secret store}"
: "${AELEV_INFRASTRUCTURE_MINIO_PROVISION_POLICY:?load the owned policy name}"
docker compose --env-file .env --profile provision run --rm bucket-provisioner
```

Пример показывает имена переменных, а не допустимые рабочие секреты. Реальные значения передаются экспортированным окружением из утверждённого хранилища секретов и не сохраняются в истории shell или Git. Не выполняйте `up` всего профиля `provision`: `bucket-provisioner` требует точного входа владельца ресурса и не является инициализатором MinIO.

## Проверка конфигурации

Python-проверка принадлежит MinIO-компоненту и находится в [`test/test_configuration.py`](test/test_configuration.py). Она проверяет единое документированное пространство `AELEV_INFRASTRUCTURE_MINIO_*`, отказ при отсутствии `.env`, восемь узлов двух площадок, TLS и файловые секреты, site replication, отсутствие прежней идентичности `object-storage` и разную политику перезапуска постоянных и одноразовых ролей. Проверка требует Python 3.13 и Docker Compose, выполняет только `docker compose config`, не запускает контейнеры и не изменяет Docker-ресурсы.

```sh
python3 test/test_configuration.py
```

## Изолированный тест жизненного цикла и отказов

[`test/multi-site-lifecycle.test.sh`](test/multi-site-lifecycle.test.sh) создаёт отдельный Compose project и временные TLS-материалы, собирает закреплённые MinIO и `mc`, поднимает восемь узлов и два локальных Nginx-балансировщика, проверяет здоровье, site replication и отдельные полномочия bucket, а затем загружает объект размером 2 ГиБ. Пока одна операция загрузки активна, тест по очереди останавливает и возвращает каждый из восьми узлов. Разрыв уже установленного TCP-соединения обрабатывается повтором S3-запроса через стабильный endpoint площадки; считать Docker DNS alias балансировщиком запрещено.

После инъекции отказов тест ждёт репликацию и сравнивает SHA-256 исходного файла и объекта на обеих площадках, подтверждает отказ кластерного healthcheck при одновременной потере двух узлов площадки, полностью останавливает и повторно запускает топологию, снова проверяет оба хеша и идемпотентность настройки site replication. В конце он явно останавливает узлы и тестовые балансировщики, затем удаляет созданные контейнеры, сети, volumes, временные файлы и только те образы, которых не было до запуска.

```sh
./test/multi-site-lifecycle.test.sh
```

Для более тяжёлого испытания размер и верхняя граница скорости меняются без правки теста, например `AELEV_MINIO_TEST_FILE_MIB=8192 AELEV_MINIO_TEST_UPLOAD_LIMIT=16Mi ./test/multi-site-lifecycle.test.sh`. Уменьшать файл для итогового подтверждения нельзя: короткое диагностическое выполнение не заменяет сохранённый результат штатного прогона. Последний подтверждённый результат находится в [`test/multi-site-lifecycle-result.md`](test/multi-site-lifecycle-result.md).

## Развёртывание на географически разнесённых серверах

Один и тот же `compose.yml`, `.env` и комплект доверия доставляются на восемь серверов. На каждом сервере запускается только принадлежащий ему процесс, например `docker compose --env-file .env up -d minio-site-a-1`. Четыре сервера площадки A используют `minio-site-a-1`…`minio-site-a-4`, четыре сервера площадки B — `minio-site-b-1`…`minio-site-b-4`. Имена из `*_NODE_*_ENDPOINT` должны разрешаться между узлами соответствующей площадки, а каждый `*_SITE_*_ENDPOINT` должен указывать на балансировщик своей площадки, а не на отдельный узел.

На разных Docker-серверах одинаковое имя локального тома не означает общий том: каждый Docker daemon создаёт собственное физическое хранилище. Сетевой диск, общий NFS-каталог или один физический диск, разделённый на четыре каталога, запрещены как имитация независимых томов. После готовности обоих пустых кластеров административная станция один раз запускает `docker compose --env-file .env -f compose.yml --profile provision run --rm --no-deps site-replication-applier`, чтобы Compose не пытался поднять удалённые узлы локально; затем владельцы ресурсов аналогично запускают `bucket-provisioner` с `--no-deps`. При первичном связывании данные могут находиться не более чем на одной площадке; остальные площадки должны быть пустыми.

Оба кластера обязаны использовать одну версию MinIO, один поставщик идентификации и совместимые корневые учётные данные. TLS-сертификат каждой площадки должен покрывать её четыре адреса узлов и адрес балансировщика, а `sites-ca.crt` должен содержать полную доверенную цепочку обеих площадок. Если применяется KMS, все площадки должны иметь доступ к совместимой службе ключей.

## Проверка и установка OpenSSL

OpenSSL нужен оператору для создания учётных данных и TLS-материала; он не устанавливается в образ MinIO, чтобы закрытые ключи не попадали в слой образа, кэш сборки или реестр образов.

```sh
if ! openssl version >/dev/null 2>&1; then
  echo "OpenSSL is not installed or cannot start" >&2
  exit 1
fi
if ! openssl req -help 2>&1 | grep -q -- "-addext"; then
  echo "OpenSSL does not support req -addext; install OpenSSL 1.1.1 or newer" >&2
  exit 1
fi
openssl version
```

На macOS с Homebrew установите OpenSSL 3 командой `brew install openssl@3` и добавьте `$(brew --prefix openssl@3)/bin` в `PATH`. На Debian или Ubuntu выполните `sudo apt-get update` и `sudo apt-get install --no-install-recommends openssl`. На Fedora выполните `sudo dnf install openssl`. После установки повторите функциональную проверку.

## Учётные данные и TLS рабочей установки

Корневые учётные данные нужны только узлам MinIO и одноразовым административным задачам. Они находятся в [`secrets`](secrets/README.md), монтируются как файлы только для чтения и не передаются через `.env` или слой образа. Site replication требует совместимых корневых учётных данных площадок; их выдаёт и доставляет утверждённое хранилище секретов.

```sh
umask 077
mkdir -p secrets/site-a secrets/site-b
openssl rand -hex 16 > secrets/root-user
openssl rand -hex 32 > secrets/root-password
chmod 600 \
  secrets/root-user \
  secrets/root-password
```

Для каждой площадки создаётся отдельный закрытый ключ и CSR. Пример для площадки A:

```sh
umask 077
MINIO_SITE_A_DOMAIN=site-a.example.com
openssl genpkey \
  -algorithm RSA \
  -pkeyopt rsa_keygen_bits:3072 \
  -out secrets/site-a/private.key
openssl req \
  -new \
  -sha256 \
  -key secrets/site-a/private.key \
  -out secrets/site-a/minio.csr \
  -subj "/CN=minio.${MINIO_SITE_A_DOMAIN}" \
  -addext "subjectAltName=DNS:minio.${MINIO_SITE_A_DOMAIN},DNS:minio-1.${MINIO_SITE_A_DOMAIN},DNS:minio-2.${MINIO_SITE_A_DOMAIN},DNS:minio-3.${MINIO_SITE_A_DOMAIN},DNS:minio-4.${MINIO_SITE_A_DOMAIN}"
chmod 600 \
  secrets/site-a/private.key \
  secrets/site-a/minio.csr
```

Повторите операцию для площадки B с её DNS-именами. Сертификат площадки вместе с промежуточной цепочкой сохраните как `site-a/public.crt` или `site-b/public.crt`. Доверенные цепочки обеих площадок объедините в `secrets/sites-ca.crt`. Перед запуском проверьте SAN, сроки и совпадение открытого ключа сертификата с закрытым ключом.

```sh
openssl x509 \
  -in secrets/site-a/public.crt \
  -noout -subject -issuer -dates -ext subjectAltName
openssl x509 \
  -in secrets/site-a/public.crt \
  -pubkey -noout | openssl pkey -pubin -outform DER | openssl sha256
openssl pkey \
  -in secrets/site-a/private.key \
  -pubout -outform DER | openssl sha256
```

Последние два значения SHA-256 должны совпасть. Без любого обязательного файла Compose прекращает создание контейнера и не создаёт каталог с именем отсутствующего файла.

## Каждая строка `.env`

Рабочий [`.env.example`](.env.example) содержит только параметры самой поставки MinIO. Все строки обязательны, запасных значений нет.

| Переменная | Локальное значение | За что отвечает и почему необходима |
|---|---|---|
| `AELEV_INFRASTRUCTURE_MINIO_COMPOSE_PROJECT` | `aelev-infrastructure-minio` | Имя проекта Compose. Изолирует контейнеры и служебные ресурсы конкретной поставки. |
| `AELEV_INFRASTRUCTURE_MINIO_SITE_A_ENDPOINT` | `https://minio-site-a:9000` | HTTPS-адрес балансировщика площадки A для административных задач и site replication. В рабочей среде обязан быть маршрутизируемым и не указывать на один узел. |
| `AELEV_INFRASTRUCTURE_MINIO_SITE_B_ENDPOINT` | `https://minio-site-b:9000` | HTTPS-адрес балансировщика площадки B. Отдельное значение необходимо для независимой географической площадки. |
| `AELEV_INFRASTRUCTURE_MINIO_BIND` | `127.0.0.1` | Интерфейс публикации портов всех локальных узлов. Для удалённых серверов задаётся защищённый маршрутизируемый интерфейс; открытие `0.0.0.0` требует firewall. |
| `AELEV_INFRASTRUCTURE_MINIO_SITE_A_NODE_1_PORT` | `19001` | Порт узла A1 на его Docker-сервере. Локально уникален из-за общей машины; на раздельных серверах может быть `9000`. |
| `AELEV_INFRASTRUCTURE_MINIO_SITE_A_NODE_2_PORT` | `19002` | Порт узла A2; отдельное значение исключает локальный конфликт портов. |
| `AELEV_INFRASTRUCTURE_MINIO_SITE_A_NODE_3_PORT` | `19003` | Порт узла A3; отдельное значение исключает локальный конфликт портов. |
| `AELEV_INFRASTRUCTURE_MINIO_SITE_A_NODE_4_PORT` | `19004` | Порт узла A4; отдельное значение исключает локальный конфликт портов. |
| `AELEV_INFRASTRUCTURE_MINIO_SITE_B_NODE_1_PORT` | `19005` | Порт узла B1; отдельное значение исключает локальный конфликт портов. |
| `AELEV_INFRASTRUCTURE_MINIO_SITE_B_NODE_2_PORT` | `19006` | Порт узла B2; отдельное значение исключает локальный конфликт портов. |
| `AELEV_INFRASTRUCTURE_MINIO_SITE_B_NODE_3_PORT` | `19007` | Порт узла B3; отдельное значение исключает локальный конфликт портов. |
| `AELEV_INFRASTRUCTURE_MINIO_SITE_B_NODE_4_PORT` | `19008` | Порт узла B4; отдельное значение исключает локальный конфликт портов. |
| `AELEV_INFRASTRUCTURE_MINIO_SITE_A_NODE_1_ENDPOINT` | `https://minio-site-a-1:9000/data` | Канонический адрес локального тома узла A1 внутри erasure set. Все четыре процесса площадки A получают один и тот же полный список. |
| `AELEV_INFRASTRUCTURE_MINIO_SITE_A_NODE_2_ENDPOINT` | `https://minio-site-a-2:9000/data` | Канонический адрес тома A2. Отсутствие узла уменьшает кворум площадки. |
| `AELEV_INFRASTRUCTURE_MINIO_SITE_A_NODE_3_ENDPOINT` | `https://minio-site-a-3:9000/data` | Канонический адрес тома A3. Он должен разрешаться со всех узлов площадки A. |
| `AELEV_INFRASTRUCTURE_MINIO_SITE_A_NODE_4_ENDPOINT` | `https://minio-site-a-4:9000/data` | Канонический адрес тома A4. Он завершает минимальный четырёхузловой erasure set. |
| `AELEV_INFRASTRUCTURE_MINIO_SITE_B_NODE_1_ENDPOINT` | `https://minio-site-b-1:9000/data` | Канонический адрес тома B1 внутри независимого erasure set площадки B. |
| `AELEV_INFRASTRUCTURE_MINIO_SITE_B_NODE_2_ENDPOINT` | `https://minio-site-b-2:9000/data` | Канонический адрес тома B2. Он не должен указывать на диск площадки A. |
| `AELEV_INFRASTRUCTURE_MINIO_SITE_B_NODE_3_ENDPOINT` | `https://minio-site-b-3:9000/data` | Канонический адрес тома B3. Он должен разрешаться со всех узлов площадки B. |
| `AELEV_INFRASTRUCTURE_MINIO_SITE_B_NODE_4_ENDPOINT` | `https://minio-site-b-4:9000/data` | Канонический адрес тома B4. Он завершает минимальный erasure set второй площадки. |
| `AELEV_INFRASTRUCTURE_MINIO_SITE_A_NODE_1_VOLUME` | `aelev-infrastructure-minio-site-a-1-data` | Точное имя постоянного локального тома A1. Изменение имени подключает новый пустой диск. |
| `AELEV_INFRASTRUCTURE_MINIO_SITE_A_NODE_2_VOLUME` | `aelev-infrastructure-minio-site-a-2-data` | Точное имя постоянного локального тома A2. Том не разделяется с другим узлом. |
| `AELEV_INFRASTRUCTURE_MINIO_SITE_A_NODE_3_VOLUME` | `aelev-infrastructure-minio-site-a-3-data` | Точное имя постоянного локального тома A3. Том не разделяется с другим узлом. |
| `AELEV_INFRASTRUCTURE_MINIO_SITE_A_NODE_4_VOLUME` | `aelev-infrastructure-minio-site-a-4-data` | Точное имя постоянного локального тома A4. Том не разделяется с другим узлом. |
| `AELEV_INFRASTRUCTURE_MINIO_SITE_B_NODE_1_VOLUME` | `aelev-infrastructure-minio-site-b-1-data` | Точное имя постоянного локального тома B1 на второй площадке. |
| `AELEV_INFRASTRUCTURE_MINIO_SITE_B_NODE_2_VOLUME` | `aelev-infrastructure-minio-site-b-2-data` | Точное имя постоянного локального тома B2 на второй площадке. |
| `AELEV_INFRASTRUCTURE_MINIO_SITE_B_NODE_3_VOLUME` | `aelev-infrastructure-minio-site-b-3-data` | Точное имя постоянного локального тома B3 на второй площадке. |
| `AELEV_INFRASTRUCTURE_MINIO_SITE_B_NODE_4_VOLUME` | `aelev-infrastructure-minio-site-b-4-data` | Точное имя постоянного локального тома B4 на второй площадке. |
| `AELEV_INFRASTRUCTURE_MINIO_PARITY` | `EC:2` | Два из четырёх блоков каждого erasure set являются блоками чётности. Это максимальная чётность четырёхтомного набора и обязательная защита от потери тома. |
| `AELEV_INFRASTRUCTURE_MINIO_NETWORK` | `aelev-infrastructure-minio` | Закрытая административная сеть узлов и одноразовых задач внутри локальной оркестрации. |
| `AELEV_INFRASTRUCTURE_MINIO_INGRESS_NETWORK` | `aelev-infrastructure-minio-ingress` | Отдельная сеть для опубликованных портов, межузловых маршрутизируемых адресов и балансировщиков. Одноразовые административные задачи подключаются к ней только для обращения к HTTPS-адресам площадок. |

Исключённый `AELEV_INFRASTRUCTURE_MINIO_CONFIG_ROOT` не был настройкой MinIO: пути к скриптам стабильны относительно Compose. Корневые учётные данные также не являются строками `.env`: закреплённый образ поддерживает `MINIO_ROOT_USER_FILE` и `MINIO_ROOT_PASSWORD_FILE`, поэтому секреты монтируются отдельными файлами.

## Фиксированные параметры Compose

| Параметр | Почему необходим |
|---|---|
| `aelev/minio-server:RELEASE.2025-04-22T22-12-26Z` | Локально собираемый vendor-образ содержит MinIO `RELEASE.2025-04-22T22-12-26Z` commit `0d7408fc9969caf07de6a8c3a84f9fbb10a6739e` и `mc` `RELEASE.2025-04-16T18-13-26Z` commit `b00526b153a31b36767991a4f5ce2cced435ee8e`. Все восемь узлов и одноразовые задачи используют один проверяемый артефакт. |
| Четыре endpoint на площадку | Включают распределённый erasure set вместо отдельного процесса с единственным диском. Каждый узел площадки получает одинаковый список. |
| `MINIO_STORAGE_CLASS_STANDARD` | Получает значение единственной переменной `AELEV_INFRASTRUCTURE_MINIO_PARITY`; для принятого четырёхтомного набора это `EC:2`, не второй источник настройки в Compose. |
| `server --certs-dir /certs` | Запускает только TLS и использует сертификат своей площадки плюс общий набор доверенных центров. |
| `MINIO_ROOT_USER_FILE` и `MINIO_ROOT_PASSWORD_FILE` | Передают корневые учётные данные через файлы только для чтения, а не через `.env`. |
| `MINIO_BROWSER=off` | Отключает постоянную браузерную Console; S3 API продолжает работать. |
| `minio-healthcheck` | Штатный `mc ready` обращается по TLS к `localhost`, доверяет только смонтированной цепочке и проверяет read/write quorum кластера, а не только наличие процесса. |
| `restart: unless-stopped` | Возвращает каждый постоянный узел после сбоя, сохраняя явную остановку оператором. |
| `site-replication-applier` | Одноразово и проверяемо связывает два уже готовых кластера; не является третьим постоянным сервисом. |
| `bucket-provisioner` | После site replication создаёт только переданный владельцем bucket, отдельного пользователя и минимальную политику; обычный MinIO не знает потребителей. |

## Данные, перенос и восстановление

Обычные `stop`, `down` и пересоздание контейнера не удаляют именованные тома. `down --volumes` удаляет восемь локальных наборов данных и запрещён для рабочей установки. Узел заменяется через штатное восстановление кворума и healing MinIO, а площадка — через site replication и resynchronization; копировать отдельные файлы или осколки объектов между томами запрещено.

TLS защищает данные в сети, но этот Compose не добавляет KMS и не шифрует физические диски сам по себе. Шифрование дисков, неизменяемые резервные копии, наблюдение за replication lag, измеренные RPO/RTO, регулярное восстановление и балансировщик с проверкой здоровья остаются обязательными свойствами площадки. Ни erasure coding, ни site replication сами по себе не заменяют резервную копию.

## Остановка и обновление

Для самостоятельной установки `docker compose --env-file .env --profile ui stop` останавливает подготовленные роли, а `down` снимает их без удаления именованных томов. Повторный `up -d --no-build --pull never` с тем же профилем использует прежние данные и identities. В общей группе выполняйте эти операции её Compose, а не сервисным файлом.

Перед обновлением сохраните согласованные настройки, удостоверения, независимую резервную копию объектов и журнал издателя. Все узлы обеих площадок должны использовать совместимый согласованный выпуск; проверьте его и порядок обновления MinIO на отдельном стенде. Не меняйте список endpoint, размер erasure set, имена томов или корневые credentials как способ «перезапуска». После обновления проверьте кворум каждой площадки, фактическую конфигурацию site replication, её задолженность и версии/хеши контрольных объектов на обеих площадках. `healthy` узлов и `ONLINE` издателя не подтверждают завершение географической репликации. Console намеренно выключена (`MINIO_BROWSER=off`), поэтому собственной браузерной панели и кнопки её открытия у этой поставки нет. Оставшиеся ворота находятся в [TASKS.md](TASKS.md).

## Собственное состояние в platform-ui

Повторная проверка названия назначения и технологического состава: [результат](test/status-labels-result.md). Она не подменяет прежние испытания жизненного цикла.

Профиль `ui` включает только собственную техническую роль публикации; обычный запуск не зависит от оболочки или IdP. Отдельная строка имеет тип `INFRASTRUCTURE`, не добавляет функциональные разделы. Состав: восемь MinIO-процессов двух площадок и собственный издатель; проверяется native write-quorum каждой ноды, но не целостность объектов/синхронность площадок. Подтверждённая отрицательная native readiness отличается от отсутствия связи: transport/TLS-ошибка означает `UNKNOWN`. При штатной остановке издатель подтверждает отписку, и строка исчезает; при исчезновении публикаций без отписки строка остаётся с «Нет связи» после срока свежести приёмника.

Поставка использует собственный образ и включённый публичный `aelev-platform-ui-client==1.1.0` в `vendor`; SHA-256 закреплён собственным Poetry lock. Это библиотека транспорта, не общий runtime-образ/каталог продуктов. Сборка после извлечения этого каталога не читает соседние исходники. Протокол, установка артефакта и пример собственного издателя: [руководство оболочки](../platform-ui/USER_MANUAL.md#отправить-метаданные-из-python).

Из этого каталога, после подготовки удостоверений:

```sh
docker compose --env-file .env --profile ui build minio-status-publisher
docker compose --env-file .env --profile provision run --rm minio-status-provision
docker compose --env-file .env --profile ui up -d --no-build minio-status-publisher
docker compose --env-file .env --profile ui stop minio-status-publisher
```

Рабочий процесс получает только `secrets/status/{shell-ca.crt,identity-ca.crt,publisher.crt,publisher.key,client-secret}`. Одноразовый `provision` получает отдельные `secrets/status-admin/{admin-user,admin-password}`, не монтируемые в runtime. Закрытые ключи CA не доставляются в контейнеры. Используйте утверждённые цепочки CA, SAN/EKU и ротацию; генерация локального удостоверения описана в руководстве и не заменяет production PKI/SVID. HTTPS-адреса могут находиться в разных странах; общей Docker-сети с UI/IdP нет. `host.docker.internal` и `keycloak:host-gateway` — только локальные адресные alias примера; для удалённой установки задайте реальные FQDN/порты и доступность сети. Проверка сертификатов не отключается.

| Переменная | Назначение |
|---|---|
| `AELEV_INFRASTRUCTURE_MINIO_UI_URL` | HTTPS-адрес платформенного технического API. |
| `AELEV_INFRASTRUCTURE_MINIO_UI_IDENTITY_URL` | HTTPS-адрес IdP; должен совпадать с SAN сертификата. |
| `AELEV_INFRASTRUCTURE_MINIO_UI_REALM` | Realm собственной машинной identity. |
| `AELEV_INFRASTRUCTURE_MINIO_UI_CLIENT_ID` | Client ID только собственной роли публикации. |
| `AELEV_INFRASTRUCTURE_MINIO_UI_AUDIENCE` | Аудитория JWT платформенного API. |
| `AELEV_INFRASTRUCTURE_MINIO_UI_MODULE_ID` | Постоянный UUID собственного описания. |
| `AELEV_INFRASTRUCTURE_MINIO_UI_INTERVAL_SECONDS` | Интервал публикации; браузер по-прежнему опрашивает раз в минуту. |
| `AELEV_INFRASTRUCTURE_MINIO_UI_TIMEOUT_SECONDS` | Предел каждой собственной сетевой пробы/отправки. |
| `AELEV_INFRASTRUCTURE_MINIO_UI_RESTART_POLICY` | Конечная политика перезапуска собственного издателя; для новой или ещё не зарегистрированной инкарнации используется `no`. |
| `AELEV_INFRASTRUCTURE_MINIO_UI_SECRET_ROOT` | Собственные рабочие удостоверения, без административных файлов. |
| `AELEV_INFRASTRUCTURE_MINIO_UI_SECRET_GID` | Дополнительная группа чтения собственных секретов. |
| `AELEV_INFRASTRUCTURE_MINIO_UI_ADMIN_ROOT` | Отдельные operator credentials только явной одноразовой задачи. |
| `AELEV_INFRASTRUCTURE_MINIO_UI_PROVISION_UID` | UID оператора для одноразовой записи собственного client secret. |
| `AELEV_INFRASTRUCTURE_MINIO_UI_STATE_VOLUME` | Постоянный журнал последовательности и точных повторяемых байтов. |
| `AELEV_INFRASTRUCTURE_MINIO_UI_MEMORY_LIMIT` | Лимит памяти только издателя. |
| `AELEV_INFRASTRUCTURE_MINIO_UI_IMAGE` | Собственный образ издателя, не общий образ инфраструктуры. |
| `AELEV_INFRASTRUCTURE_MINIO_UI_PYTHON_IMAGE` | Закреплённый Python 3.13.7 builder/runtime издателя. |

Журнал `UI_STATE_VOLUME` сохраняется при stop/start и обновлении; не удаляйте его как кэш: приёмник помнит sequence прежнего UUID. Восстановление требует собственного backup журнала или согласованного нового UUID, не сброса чужой БД/повторного sequence=1. Состояние оболочки восстанавливается повторной публикацией, canonical источник описания/состава остаётся у владельца.

`test/status_publication.py` работает с уже существующей настоящей установкой: OIDC/API/DOM, stop/start частей и издателя, реальное истечение TTL, возврат исходных контейнеров. Пример: `python3.13 test/status_publication.py --ui-url https://<ui-host>:<port> --viewer-user <viewer> --viewer-password-file <password-file> --chromium <installed-chromium> --status-ttl-seconds 120`. Требуются Python 3.13 и Playwright 1.56.0 в операторской тестовой среде; runtime-образ их браузерную роль не запускает. Исключение браузерной проверки сертификата допустимо только явным `--allow-local-certificate` на loopback с локальной PKI; рабочие издатели всегда проверяют TLS. Тест не создаёт фиктивных identities, каталогов, контейнеров, сетей или томов и восстанавливает исходное состояние; отчёт/снимок остаются в `test`. Полный остановленный приёмник UI физически не может доставить собственный свежий отчёт: проверяется UNKNOWN в уже открытом браузере и восстановление после старта.

Результаты текущей проверки публикации состояния: [читаемый отчёт](test/status-validation-result.md), [проверка нативных частей](test/status-native-result.md), [проверка издателя](test/status-publication-result.md). Прежние предметные/кластерные испытания не подменяются этой проверкой.

## Обновление журнала издателя

Сборка собственного издателя использует закреплённую SQLite 3.53.4; ОС, Python и SDK не заменяются. До первого запуска задайте `AELEV_INFRASTRUCTURE_MINIO_UI_RESTART_POLICY=no`. При обновлении сначала удержите точного предшественника через [существующий ограничитель](../USER_MANUAL.md#ограниченное-восстановление-по-unhealthy), остановите только издателя и сохраните полный его том с WAL, inspect и журналы. Не вызывайте отписку, не удаляйте том и не меняйте UUID.

На изолированных копиях проверьте целостность, схему и все строки старым и новым движками. Запускайте только проверенный издатель с прежними mounts и `restart=no`, без запуска зависимостей. После healthy передайте прежний бюджет штатным `replace`; только для действительно незарегистрированной истории с подтверждёнными нулевыми рестартами допустим `enroll`. Значение env оставьте `no`: фактический оставшийся бюджет выставляет ограничитель, а новая инкарнация должна пройти передачу истории снова. Исчерпанный или заблокированный бюджет не сбрасывайте. Перед следующей заменой повторяйте эту процедуру, а не обычный пересоздающий `up`.

## Лёгкая проба издателя

При обновлении сохраняйте фактическое имя Compose-проекта работающего издателя и передавайте его явно через `-p`. Имя самостоятельной группы из примера не подставляется вместо действующей общей группы: два издателя одного журнала одновременно запрещены.

Соберите собственный совместимый образ с закреплённым клиентом Platform UI 1.1.1. Закрепите его в `AELEV_INFRASTRUCTURE_MINIO_UI_IMAGE` и проверьте Compose. До адресного обновления только `minio-status-publisher` сохраните согласованную SQLite-копию собственного журнала и конфигурацию; не удаляйте том, UUID, sequence или ожидающие байты. Одной замены команды healthcheck без совместимого образа недостаточно. Применяйте `up -d --no-deps --no-build --pull never` только к издателю с профилем `ui`, затем проверьте свежий heartbeat, продвижение sequence и доставку состояния. Нативные процессы, данные, секреты и бюджеты восстановления не меняются. Точные свойства пробы определены в [SPEC](SPEC.md); её успех не заменяет проверки готовности и доставки.

При откате остановите только новый издатель и выберите сохранённый совместимый образ с прежней командой пробы, не меняя журнал. Схема журнала этим обновлением не меняется; возврат более старого клиента с несовместимым форматом без отдельной подготовки не допускается. Если процесс не восстановился, сохраняйте журналы и безопасную причину ошибки, не обнуляйте последовательность и не увеличивайте таймаут вместо устранения причины.

Исключение секретных файлов из публичного контекста проверяет `test/Dockerfile.publisher-context`. Задайте в переменной окружения `AELEV_INFRASTRUCTURE_MINIO_UI_PYTHON_IMAGE` точное закреплённое значение из собственного `.env.example` и выполните `docker build --network none --build-arg PYTHON_IMAGE="$AELEV_INFRASTRUCTURE_MINIO_UI_PYTHON_IMAGE" -f test/Dockerfile.publisher-context .`. Проверка монтирует контекст в режиме только чтения, не копирует его в слои образа и требует, чтобы из `secrets` был виден только публичный `README.md`; сертификаты, ключи и пароли не должны попадать в контекст.
