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

`test/test_fencing_node.py`, `test/test_fencing_ssh.py`, `test/test_fencing_launcher.py` покрывают подмену цели, произвольные команды, потерянные ответы, ручную остановку, конкуренцию, сохранение трёх попыток и transport bounds. Реальный SSH/Docker тест выполняется только на новых помеченных ресурсах и явно указывает, использован ли настоящий DCS. Несколько SSH-процессов одной Docker VM считаются одним физическим узлом. Для выпуска нужны signed digests обеих CPU, security admission, настоящие PostgreSQL/Patroni/etcd failover/replay, backup/restore и распределённая приёмка.

Официальные основания: [Docker socket](https://docs.docker.com/engine/security/protect-access/), [OpenSSH forced command/restrict](https://man.openbsd.org/sshd.8), [Patroni pre_promote](https://patroni.readthedocs.io/en/latest/yaml_configuration.html).
