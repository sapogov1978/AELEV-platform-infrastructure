# Независимое размещение Docker fencer: кандидат

Профиль реализует размещение по [00-INFRA-0018](../docs/adr/00-INFRA-20261113-0018-independent-roles-and-translation-mtls.md): HTTPS/mTLS executor, его копии и каждый SQL-узел могут находиться на разных машинах. Допуск образов, автоматизированная установка и полное HA-испытание ещё требуются. Исходный Docker-профиль и рабочие установки сохраняются; общие гарантии определены в [FENCING.md](FENCING.md).

## Контракт и владение

Поток: `pre_promote → HTTPS/mTLS fencer → SSH forced command узла → одноразовый Docker helper → собственный Docker Engine`. Запрос содержит только `schema`, `cluster`, `node`, `daemon_id`, `action`, `request_id` и native `claim` для `fence`. Доступны `status`, `fence`, `return-once`; команда, image, paths, environment и DCS endpoints не принимаются из запроса. SSH используется только для технической операции, предметный транспорт остаётся Kafka.

Канонический журнал связан с cluster/node/Engine ID на управляемом узле. Все копии контроллера используют эту операцию и общий файловый барьер; их собственные audit volumes не содержат отдельных бюджетов возврата. Прежний `IntentStore` сохраняет до эффекта точные container ID/StartedAt, ручную остановку и три попытки. Возврат использует прежние согласованные linearizable чтения DCS и повторные проверки перед start. Между компьютерами не требуется общий filesystem; новая аренда или отдельный выбор ведущего не вводятся.

Helper запускается на время запроса через административный SSH/Docker узла. Постоянная дополнительная служба на SQL-хосте не нужна. Недоступность SSH, host-key mismatch, смена Engine, отсутствующий binding и неопределённая native-аренда запрещают подтверждение; переход к локальному socket, паролю или HTTP не выполняется. Недоступный по SSH хост не считается физически ограждённым: ограничение доступности этого механизма проверяется при site admission.

## Образы

Dockerfile содержит targets `fencing-node` и `fencing-ssh` на закреплённых Python 3.13.7 / Alpine 3.22. Первый предоставляет узловую операцию на стандартной библиотеке; второй добавляет `openssh-client=10.0_p1-r11` и SSH adapter. Отдельные fencing targets не загружают Patroni, psycopg или PostgreSQL server. Default target `runtime` сохраняет SQL-поставку. Добавление OpenSSH является кандидатом изменения состава runtime и требует согласования принятого стека до поставки.

Проверенный ARM-кандидат также содержит security updates musl 1.2.5-r13, libuuid 2.41.6-r1, busybox 1.37.0-r20, xz-libs 5.8.4-r0, OpenSSL 3.5.9-r0 и zlib 1.4-r0; pip/ensurepip удалены из fencing runtime. Переход zlib с 1.3 на 1.4 включён в отдельное согласование состава. SHA образа, полный raw scan без suppressions и границы проверок фиксируются в [отчёте кандидата](../docs/audit/postgresql-fencing-ssh-20261114.json). Это не изменение SQL image или рабочего стенда.

## Узловая операция

Ручной workflow `release-postgresql-fencing-image` подготавливает GHCR image `aelev-platform-infrastructure-postgresql-fencing` из target `fencing-ssh` для AMD64/ARM64, SBOM, provenance и Cosign signature точного digest. Он не запускается от push/tag и требует завершённых успешных `audit` и `postgresql-fencing-ssh-native` той же исходной версии; более поздний failed/pending run не заменяется старым успешным. Публикация выполняется после согласования состава, а допуск exact registry image обеих CPU, его native повтор и переключение установки остаются отдельными шагами. Один такой образ может использоваться controller и одноразовым helper; `fencing-node` является его минимальной build-основой, а не заявлением об отдельно опубликованном артефакте.

На каждом Linux-узле нужны Docker CLI `/usr/bin/docker`, Python 3.13 и административный SSH. Root-owned launcher устанавливается в `/opt/aelev/postgresql/fencing-node.py` со сверкой source SHA. Root-only `/etc/aelev-postgresql-fencing/node.json` имеет mode 0600; каталог secrets — 0700. Структура с placeholders:

```json
{
  "schema": 1,
  "cluster": "example-cluster",
  "node": "postgresql-2",
  "daemon_id": "REPLACE_WITH_EXACT_DOCKER_ENGINE_ID",
  "image": "ghcr.io/owner/fencing-node@sha256:REPLACE_WITH_ADMITTED_DIGEST",
  "state_volume": "example-cluster-node-2-fencing",
  "secrets_root": "/etc/aelev-postgresql-fencing/secrets",
  "etcd": [
    {"host": "etcd-1.example.invalid", "port": 2379},
    {"host": "etcd-2.example.invalid", "port": 2379},
    {"host": "etcd-3.example.invalid", "port": 2379}
  ],
  "timeout_seconds": 20
}
```

Secrets содержат только собственные `etcd-ca.crt`, `etcd-client.crt`, `etcd-client.key`. CA private key, PGDATA и чужие credentials не доставляются. Образ предварительно загружается по точному digest для native CPU. Launcher проверяет Engine ID, существующий именованный volume и метки `io.aelev.postgresql.cluster` / `io.aelev.postgresql.node`; отсутствующий том не создаётся неявно.

Вместо `secrets_root` разрешён явный `secrets_volume` с именем собственного предварительно подготовленного тома. Эти поля взаимоисключающие. Том credentials отличается от журнала и несёт те же cluster/node labels плюс `io.aelev.postgresql.fencing-resource=credentials`; launcher сверяет их до запуска и монтирует credentials только read-only. Он не создаёт отсутствующий том и не переключается между file/volume при ошибке. Владелец подготавливает внутри тома каталог mode 0700 и файлы mode 0600 для UID runtime, чтобы helper читал ключи с обычным `cap_drop=ALL`.

Для новой установки владелец отдельно создаёт помеченный volume и выполняет `python3.13 /opt/aelev/postgresql/fencing-node.py --initialize`. Допускается пустая история или повтор прежнего binding. Существующие локальные намерения требуют отдельного handoff с сохранением журнала и бюджета; автоматическое обнуление/миграция не реализованы. Обычный SSH-запрос не может инициализировать новую историю.

Выделенная SSH identity получает только forced command, например `restrict,command="/usr/bin/sudo -n -- /usr/bin/python3.13 /opt/aelev/postgresql/fencing-node.py"` в `authorized_keys`. Отдельное sudo-правило разрешает ровно эту команду без дополнительных arguments; launcher/configuration недоступны для изменения SSH-пользователю. Password/interactive authentication, forwarding, tunnels, PTY и user rc отключаются. Docker socket предоставляет административные полномочия; обычный shell или произвольный Docker API этой identity не выдаются. Каждая копия контроллера имеет собственный закрытый ключ, допущенный владельцем узла.

## Контроллер

Самостоятельный `compose.fencing-ssh.yml` запускает только fencer без SQL/etcd и Docker socket. Нужны собственные TLS/SSH inputs в `secrets/fencing`, отдельный `FENCING_SSH_AUDIT_VOLUME` и обычные адреса трёх etcd. `ssh-bindings.json` mode 0600 перечисляет допущенные узлы; для работы со всеми тремя каждый должен иметь запись:

```json
{
  "postgresql-2": {
    "host": "node-2.example.invalid",
    "port": 22,
    "user": "aelev-fencing",
    "identity_file": "/run/aelev/secrets/node-2.key",
    "known_hosts_file": "/run/aelev/secrets/known_hosts",
    "daemon_id": "REPLACE_WITH_EXACT_DOCKER_ENGINE_ID"
  }
}
```

SSH host keys поступают через доверенный канал подготовки; TOFU и `ssh-keyscan` не используются как источник доверия. `/health` возвращает 503 до свежего подтверждения допущенных узлов, при их потере или остановке return loop. Положительный ответ подтверждает работоспособность executor transport; SQL primary, synchronous COMMIT, backup и site admission проверяются отдельно.

## Проверки и ограничения

Поздний AMD64 run `43653529360` source `b17f01c` остановился до SQL restore: healthy кластер с единственным writer не подтвердился после Patroni fault. Его причина не установлена; прежний успешный HA run `43652728730` source `09892fb` хранится отдельно и не закрывает поздний отказ. Следующая диагностика выполняет три независимых ограниченных trials и добавляет в safe proof только process/health state, фиксированные reason-code counts и разрешённые фазы/бюджеты журналов. Raw logs, ключи, Docker configuration и canonical journal payloads в CI artifact не включаются; timeout и runtime guards сохраняются.

После HA/crash/replay сценарий снимает нативный `pg_basebackup` по published primary с прежним `verify-full` mTLS, проверяет manifest через `pg_verifybackup` и запускает отдельный восстановленный SQL-процесс на непубликуемом `127.0.0.1:16432`. Требуются те же 1000 строк и SHA256, отсутствие recovery mode и сохранность прежнего primary/quorum. Это проверка механизма physical restore на синтетическом кластере; независимое backup-хранилище, расписание, WAL/PITR и полный аварийный запуск площадки ею не подтверждаются.

Публичный `test/fencing-ssh-lifecycle.py` проверяет настоящие PostgreSQL/Patroni, три etcd и две копии fencer. Одиннадцать долгоживущих тестовых ролей, включая три административных SSH host fixtures, получают отдельные Compose projects/networks; обмен идёт через published node endpoints. SQL/etcd/fencer используют канонические owner recipes, из которых диагностический exporter выбирает одну роль и её собственные ресурсы. Это диагностический адаптер, не полный production installer. Клиентские ключи контроллеров, узловых операций и SQL-реплик отдельные; helper/coordinator работают с обычным `cap_drop=ALL`, private inputs предварительно материализуются в своих томах.

Тест подтверждает 1000 строк, замораживает Patroni при ещё живом SQL primary, проверяет настоящее ограждение прежнего процесса и нового ведущего с тем же хешем данных. Затем теряет ответ одного RPC, останавливает первый coordinator, возвращает точный узел через второй и пересоздаёт первый с сохранением бюджета. До cleanup сохраняются пять собственных technical journal archives; проверяется наличие обеих peer termination receipts. Ключи и private native logs остаются только в закрытом output, не публикуются CI. Работающий контур защищён уникальными namespaces, проверкой ownership и сверкой прежних non-ephemeral containers.

Для повтора сначала соберите три native image targets из того же checkout и загрузите закреплённый etcd image из `compose.runtime.yml`. `checks` используется только как база административного SSH host fixture; SQL и fencing работают в своих runtime targets. Output обязан находиться вне checkout:

```sh
docker build --target runtime --build-context tests=./test --iidfile /var/tmp/pg-ssh-sql.image-id .
docker build --target checks --build-context tests=./test --iidfile /var/tmp/pg-ssh-checks.image-id .
docker build --target fencing-ssh --build-context tests=./test --iidfile /var/tmp/pg-ssh-fencer.image-id .
python3.13 test/fencing-ssh-lifecycle.py \
  --output /var/tmp/pg-ssh-acceptance-new \
  --sql-image "$(cat /var/tmp/pg-ssh-sql.image-id)" \
  --fencing-image "$(cat /var/tmp/pg-ssh-fencer.image-id)" \
  --ssh-host-base-image "$(cat /var/tmp/pg-ssh-checks.image-id)"
```

Проверяющий процесс должен работать на узле выбранного Docker Engine с доступом к тем же filesystem inputs; для Docker Desktop используются его явные host endpoints, для native Linux — опубликованный адрес локального bridge interface. Это два локальных окружения с теми же TLS/SSH/SQL контрактами, а не проверка разных физических машин. Native AMD64 workflow собирает images и выполняет тот же сценарий без QEMU; он сохраняет только безопасный `proof.json` и image evidence и не публикует runtime images.

`test/test_fencing_node.py`, `test/test_fencing_ssh.py`, `test/test_fencing_launcher.py` покрывают подмену цели, произвольные команды, потерянные ответы, ручную остановку, конкуренцию, сохранение трёх попыток и transport bounds. Native ARM публичный сценарий с настоящим DCS прошёл; [отчёт HA](../docs/audit/postgresql-fencing-ha-20261114.json) сохраняет точные inputs, hashes и ограничения отдельно от раннего опыта с имитацией DCS. Результаты других CPU учитываются только после завершённого workflow. Несколько SSH-процессов одной Docker VM считаются одним физическим узлом. Для выпуска ещё нужны signed digests обеих CPU, security admission, полный node installer/history handoff, SQL backup/restore и распределённая приёмка.

Официальные основания: [Docker socket](https://docs.docker.com/engine/security/protect-access/), [OpenSSH forced command/restrict](https://man.openbsd.org/sshd.8), [Patroni pre_promote](https://patroni.readthedocs.io/en/latest/yaml_configuration.html).
