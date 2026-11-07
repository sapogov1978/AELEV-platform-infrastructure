# Clean-room воспроизведение полного локального профиля

Этот runbook предназначен для инженера, который не знает внутренней истории репозитория. Результат считается воспроизведённым только после сборки из точного commit, запуска полного локального состава, прохождения одиннадцати функциональных проверок и проверки неизменяемого evidence. Наличие документации, успешный build или `healthy` контейнеров по отдельности не дают статус `PRODUCTION_READY`.

Профиль `full-local` относится только к техническим функциям Platform Infrastructure. Он не создаёт прикладные topics, buckets, базы, политики, identities или ресурсы потребителей. Допуск production-площадки по-прежнему выполняется отдельно.

## Требования к чистой машине

- Linux Docker Engine либо Docker Desktop с Linux containers, Docker Compose 2.24.4+, Python 3.13, Poetry 2.2.1, OpenSSL и Chromium;
- чистый checkout точного release commit без существующих контейнеров, сетей и volumes этого Compose project;
- место и ресурсы по руководствам владельцев;
- четыре заранее допущенных digest-pinned образа: непрозрачного translation provider из отдельного private-репозитория [`AELEV-translation-provider`](https://github.com/sapogov1978/AELEV-translation-provider), Translator worker, owner-controlled Observability Sender и Telemetry Delivery, выпускаемые workflow этого репозитория;
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

`git status --porcelain` должен быть пустым. `plan` обязан перечислить 15 владельцев, 55 корневых и две compute-роли, три внешних входа, один build input, один release image и одиннадцать функций приёмки. Канонический машиночитаемый состав находится в [`full-local.json`](../../full-local.json); ручное исключение роли создаёт другой профиль.

## 2. Инициализировать и собрать без запуска

После `init` в `translator-service/.env` оператор указывает `AELEV_INFRASTRUCTURE_TRANSLATOR_PROVIDER_IMAGE` и `AELEV_INFRASTRUCTURE_TRANSLATOR_WORKER_IMAGE` только как `repository@sha256:<64 hex>`. Допущены Linux amd64 provider `ghcr.io/sapogov1978/aelev-translation-provider@sha256:4a820a13e6a03188589c09d14e52b5f2d05e9359d7df847bad768966016e9ec9` из artifact [provider release 42248923795](https://github.com/sapogov1978/AELEV-translation-provider/actions/runs/42248923795) и worker `ghcr.io/sapogov1978/aelev-platform-infrastructure-translator-worker@sha256:e006ab2466b6455d6a928e8aa9aa12fc023ac9a7e3c5897082508249b422f912` из artifact [worker release 42259587979](https://github.com/sapogov1978/AELEV-platform-infrastructure/actions/runs/42259587979). Первый образ реализует непрозрачный приватный порт перевода; Translator не получает модель, её версию или способ исполнения. Все Dockerfile-потребители SQLite/sender runtime закрепляют multi-architecture build input `ghcr.io/sapogov1978/aelev-platform-infrastructure-observability-sender@sha256:162f07e6fd043eb3d9e04e7c415bee07771321a6f7f3864fcba0ce1f80dd941e` из подписанного [sender release 42308900143](https://github.com/sapogov1978/AELEV-platform-infrastructure/actions/runs/42308900143). Для private GHCR clean runner заранее получает только `read:packages`; credential не пишется в `.env`, Git или evidence. Рабочие `.env` не коммитятся.

```sh
python3 tools/full_local.py init
# установить два точных digest reference в translator-service/.env
python3 tools/full_local.py build
```

`init` создаёт только отсутствующие локальные env/PKI прежними владельческими средствами и не перезаписывает существующие файлы. Только в новых env локального профиля он привязывает `*_SECRET_GID` и `*_PROVISION_UID` к identity текущего host-оператора. Для одноразовых задач clean-room он create-only доставляет сгенерированного локального deployment-admin Keycloak в отдельный `UI_ADMIN_ROOT` каждого владельца: общего secret mount нет, существующий файл не заменяется, содержимое не попадает в evidence. Уже существующие настройки и secrets площадки остаются неизменными; production получает эти файлы от внешнего secret manager по руководству владельца. `build` собирает все buildable-роли, три локальные SPIRE target и получает отсутствующие pull-only образы исключительно по digest; контейнеры не запускаются. Это сохраняет инвариант «build не выполняет up».

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

Код `0` и `status=READY` означают только, что все env и owner input paths существуют, оба Compose рендерятся в точный service set, внешние образы заданы digest, локальные образы присутствуют, pull-only images неизменяемы и одноразовое подтверждение SPIRE присутствует в этом процессе.

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
8. полный Kafka → Translator → непрозрачный provider → Kafka перевод без сведений о модели;
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

Ручной workflow `clean-room-full-local` создаёт одноразовый GitHub-hosted Ubuntu runner, получает три exact digest из версионируемого manifest, использует созданную `init` временную operator identity и уничтожает всю машину после job. Пользовательский сервер, постоянный runner, repository variables и передача пароля в GitHub Secrets не требуются. `GITHUB_TOKEN` имеет только `contents:read` и `packages:read`; private provider package должен предоставить этому репозиторию Actions read access через **Package settings → Manage Actions access → Add repository**, как предписывает [официальное руководство GitHub](https://docs.github.com/en/packages/learn-github-packages/configuring-a-packages-access-control-and-visibility#ensuring-workflow-access-to-your-package). Обычный PR CI выполняет только структурные проверки и не выдаёт функциональное evidence.

## 7. Решение Production Ready

Статус репозитория может быть `PRODUCTION_READY` для `full-local` только когда на точном release commit одновременно проходят repository CI, preflight, BOM и новое clean-room evidence. Затем площадка отдельно подтверждает PKI/KMS, DNS/firewall/WAN, capacity, failure domains, backup target, alert delivery, RPO/RTO, подпись и admission образов. Ни один из этих выводов не разрешает `RESUME`, миграцию либо воздействие на существующий runtime.
