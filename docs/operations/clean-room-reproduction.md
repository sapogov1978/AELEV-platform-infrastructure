# Clean-room воспроизведение полного локального профиля

Этот runbook предназначен для инженера, который не знает внутренней истории репозитория. Результат считается воспроизведённым только после сборки из точного commit, запуска полного локального состава, прохождения одиннадцати функциональных проверок и проверки неизменяемого evidence. Наличие документации, успешный build или `healthy` контейнеров по отдельности не дают статус `PRODUCTION_READY`.

Профиль `full-local` относится только к техническим функциям Platform Infrastructure. Он не создаёт прикладные topics, buckets, базы, политики, identities или ресурсы потребителей. Допуск production-площадки по-прежнему выполняется отдельно.

## Требования к чистой машине

- Linux Docker Engine либо Docker Desktop с Linux containers, Docker Compose 2.24.4+, Python 3.13, Poetry 2.2.1, OpenSSL и Chromium;
- чистый checkout точного release commit без существующих контейнеров, сетей и volumes этого Compose project;
- место и ресурсы по руководствам владельцев;
- два заранее допущенных digest-pinned owner-controlled образа: Observability Sender и Telemetry Delivery, выпускаемые workflow этого репозитория, а также официальный digest-pinned Python test-driver для изолированной telemetry-приёмки; реализация перевода не является входом `full-local`;
- отдельное операторское решение на единственный запуск SPIRE installer с Docker socket; подтверждение не сохраняется в `.env`;
- локальные secrets/PKI и внешние owner inputs, подготовленные владельческими средствами. Они не передаются в Git и не печатаются в evidence.

Официальная установка Docker описана в [Docker Engine installation](https://docs.docker.com/engine/install/), Compose — в [Compose plugin installation](https://docs.docker.com/compose/install/), проверка образов по digest — в [Docker image digests](https://docs.docker.com/dhi/core-concepts/digests/).

## 1. Зафиксировать исходники

```sh
git clone https://github.com/sapogov1978/AELEV-platform-infrastructure.git
cd AELEV-platform-infrastructure
git checkout <RELEASE_COMMIT>
git status --porcelain
python3 tools/full_local.py validate
python3 tools/full_local.py plan > full-local-plan.json
```

`git status --porcelain` должен быть пустым. `plan` обязан перечислить 15 владельцев, 55 корневых ролей, один внешний операторский вход, один внешний digest-pinned test-driver, один build input, один release image и одиннадцать функций приёмки. Provider/worker-ячейка отсутствует: способ исполнения перевода не выбран и не принадлежит обязательной композиции. Канонический машиночитаемый состав находится в [`full-local.json`](../../full-local.json); ручное исключение роли создаёт другой профиль.

## 2. Инициализировать и собрать без запуска

Все Dockerfile-потребители SQLite/sender runtime закрепляют multi-architecture build input `ghcr.io/sapogov1978/aelev-platform-infrastructure-observability-sender@sha256:162f07e6fd043eb3d9e04e7c415bee07771321a6f7f3864fcba0ce1f80dd941e` из подписанного [sender release 42308900143](https://github.com/sapogov1978/AELEV-platform-infrastructure/actions/runs/42308900143). Telemetry Delivery также закреплён manifest по digest. Изолированный onboarding Grafana использует официальный [Python 3.13.7 Alpine image](https://hub.docker.com/_/python) по точному digest из manifest; это acceptance test-driver, а не runtime-сервис. `build` получает его заранее, а сам тест не скачивает не объявленные входы. Для private GHCR clean runner заранее получает только `read:packages`; credential не пишется в `.env`, Git или evidence. Рабочие `.env` не коммитятся. Provider credentials и image references не требуются, поскольку `full-local` проверяет сохранение очереди без реализации перевода.

```sh
python3 tools/full_local.py init
python3 tools/full_local.py build
```

`init` создаёт только отсутствующие локальные env/PKI прежними владельческими средствами и не перезаписывает существующие файлы. Только в новых env локального профиля он привязывает `*_SECRET_GID` и `*_PROVISION_UID` к identity текущего host-оператора. Только для заново созданных входов он также назначает этой группе чтение `0750/0640` на точные runtime mounts Translator и `secrets/fencing` PostgreSQL; закрытый Translator CA key остаётся за пределами runtime mount, а существующие деревья площадки не меняются. Для одноразовых задач clean-room он create-only доставляет сгенерированного локального deployment-admin Keycloak в отдельный `UI_ADMIN_ROOT` каждого владельца и только публичную CA локального IdP в объявленный отдельный `OIDC_CA_FILE` либо как `identity-ca.crt` в явно объявленный `OPERATOR_SECRET_ROOT`: закрытый CA key и client secret не копируются, существующий trust не заменяется. Закрытые secret roots получают `0750/0640`, отдельный публичный trust — `0755/0644`; общего secret mount нет, содержимое credentials не попадает в evidence. Уже существующие настройки и secrets площадки остаются неизменными; production получает эти файлы от внешнего secret manager/PKI по руководству владельца. `build` собирает все buildable-роли, три локальные SPIRE target и получает отсутствующие pull-only образы исключительно по digest; контейнеры не запускаются. Это сохраняет инвариант «build не выполняет up».

Для полного локального профиля `init` также create-only выдаёт каждому объявленному `UI_SECRET_ROOT` отдельную publisher identity от локальной CA Platform UI, публичные цепочки UI/IdP и отдельные owner-probe identities там, где технический health использует mTLS. Закрытые issuer keys остаются только у operator-side генератора и не монтируются в workload; `client-secret` по-прежнему создаёт только собственная status-provision задача. Это локальная PKI на 30 дней, не production admission и не автоматическая ротация.

`build` также получает объявленные build input images в локальный Docker image store. Telemetry onboarding использует тот же закреплённый Sender SDK с native Prometheus, а не runtime центрального Delivery с другим ENTRYPOINT; одного BuildKit cache для этого недостаточно. Preflight проверяет присутствие SDK, а build-input раздел BOM фиксирует его фактический image ID.

Новый operational `observability/.env` выбирает владельческие `.env`, тогда как версионируемый `.env.example` сохраняет `.env.example` только для проверки шаблонов. Уже существующий выбор оператора bootstrap не меняет.

В локальном профиле собственный `keycloak-status-provision` обращается к Keycloak по внутреннему TLS origin `https://keycloak:8443`; браузерный `PUBLIC_URL` и внешний host bind не меняются. Bootstrap переводит только прежний локальный default `https://127.0.0.1:18443`, но сохраняет любой явно заданный оператором endpoint.

## 3. Подготовить owner inputs

`full_local.py init` вызывает владельческие генераторы для отсутствующих local PKI/runtime, создаёт owner-local output directories и create-only файлы локального deployment-admin для provision-задач и закрыто останавливается при частичном PKI. Существующие `.env`, ключи, admin-файлы и secret roots не заменяются. Для нестандартных либо production identities выполните соответствующие разделы руководств [Keycloak](../../keycloak/USER_MANUAL.md), [PostgreSQL](../../postgresql/USER_MANUAL.md), [MinIO](../../minio/USER_MANUAL.md), [Observability](../../observability/USER_MANUAL.md), [Public Egress Gateway](../../public-egress-gateway/USER_MANUAL.md), [SPIRE](../../spire/USER_MANUAL.md), [Platform UI](../../platform-ui/USER_MANUAL.md) и [Translator](../../translator-service/USER_MANUAL.md). Не создавайте пустые файлы вместо удостоверений и не копируйте целые secret roots между владельцами.

SPIRE installer запускается только в отдельном процессе с одноразовым окружением:

```sh
DRP_SPIRE_DOCKER_SOCKET_APPROVAL=operator-root-equivalent-v1 \
  sh spire/install.sh server aelev-infrastructure-spire \
  aelev/infrastructure-spire:local \
  aelev/infrastructure-spire-installer:local \
  --environment local --trust-domain local.example
```

Не экспортируйте approval в shell profile и не записывайте его в `.env`. Повторная установка поверх существующего состояния выполняется только по SPIRE manual.

## 4. Выполнить исчерпывающий preflight

```sh
DRP_SPIRE_DOCKER_SOCKET_APPROVAL=operator-root-equivalent-v1 \
  python3 tools/full_local.py preflight --require-images \
  > clean-room-preflight.json
```

Код `0` и `status=READY` означают только, что все env и owner input paths существуют, корневой Compose рендерится в точный service set, внешние образы заданы digest, локальные образы присутствуют, pull-only images неизменяемы и одноразовое подтверждение SPIRE присутствует в этом процессе.

Код `2` сопровождается ограниченными `blocking_codes` без значений секретов. Исправляйте причину у владельца; не удаляйте проверку и не подменяйте digest tag-ом.

## 5. Сформировать release BOM

На чистом release commit, после получения всех образов:

```sh
DRP_SPIRE_DOCKER_SOCKET_APPROVAL=operator-root-equivalent-v1 \
  python3 tools/full_local.py bom --output evidence/release-bom.json
```

BOM закрепляет source commit, канонические digest отрендеренных Compose models, image IDs, доступные repository digests, service mapping, состояние внешних входов и точные owner-controlled build inputs с их release evidence и потребителями. По умолчанию грязное дерево отклоняется. `--allow-dirty` существует только для разработки и такое evidence не допускается как release/Production Ready.

## 6. Функциональная приёмка

Полный прогон обязан получить отдельное доказательство для каждой функции из `full-local.json`:

1. реальный OIDC login;
2. отображение всех owner statuses в Platform UI;
3. Kafka publish/consume round trip;
4. PostgreSQL transaction commit/read;
5. запись/чтение объекта и проверка digest;
6. default-deny и один exact-allow Public Egress Gateway;
7. доставка и запрос telemetry;
8. приём, дедупликация и сохранение обеих очередей Translator без выбранного провайдера, включая полный restart и отказ Kafka;
9. настоящая выдача, ротация и отзыв workload identity;
10. сбор, durable delivery, outage/recovery и Grafana-проекция наблюдения SPIRE;
11. ограниченное восстановление с сохранением состояния.

Runner сам устанавливает lock-pinned test drivers, выполняет owner lifecycle-сценарии последовательно, сохраняет внешние отчёты и логи, вычисляет их SHA-256, сверяет Docker baseline и повторно проверяет чистоту source tree. Каталог evidence должен находиться вне checkout и быть пустым:

```sh
DRP_SPIRE_DOCKER_SOCKET_APPROVAL=operator-root-equivalent-v1 \
  python3 test_infrastructure/clean_room_gate.py \
  --output /var/tmp/aelev-clean-room-evidence \
  --viewer-user <VIEWER_USER> \
  --viewer-password-file /run/secrets/aelev-viewer-password \
  --chromium /path/to/chromium \
  --allow-local-certificate
```

Пароль передаётся только путём к локальному файлу и не попадает в аргументы subprocess, отчёты либо Git. Невыполненный, пропущенный или частично успешный этап создаёт `blocked-run.json`, возвращает код `2` и не создаёт PASS evidence. После каждого теста созданные ресурсы снимаются адресно; исходные containers, их состояния, volumes, networks, images, secrets и runtime должны сохраниться.

Проверка готового evidence выполняется без доверия к названиям файлов:

```sh
python3 test_infrastructure/clean_room.py \
  --bom evidence/release-bom.json \
  --evidence evidence/clean-room-evidence.json
```

Verifier сверяет точный набор функций, release/source, digest BOM, SHA-256 каждого артефакта, чистый baseline, успешную сохранность cleanup и итог `PASS`. Он не превращает старые отчёты в новый прогон.

Ручной workflow `clean-room-full-local` создаёт одноразовый GitHub-hosted Ubuntu runner, получает exact digests из версионируемого manifest, использует созданную `init` временную operator identity и уничтожает всю машину после job. Пользовательский сервер, постоянный runner, repository variables и передача пароля в GitHub Secrets не требуются. `GITHUB_TOKEN` имеет только `contents:read` и `packages:read`. Обычный PR CI выполняет только структурные проверки и не выдаёт функциональное evidence.

## 7. Решение Production Ready

Статус репозитория может быть `PRODUCTION_READY` для `full-local` только когда на точном release commit одновременно проходят repository CI, preflight, BOM и новое clean-room evidence. Затем площадка отдельно подтверждает PKI/KMS, DNS/firewall/WAN, capacity, failure domains, backup target, alert delivery, RPO/RTO, подпись и admission образов. Ни один из этих выводов не разрешает `RESUME`, миграцию либо воздействие на существующий runtime.
