# 00-INFRA-20261113-0018-independent-roles-and-translation-mtls — Независимое размещение ролей и mTLS Translator

- **Статус:** Принят по подтверждению владельца от 2026-11-13
- **Дата:** 2026-11-13
- **Уточняет:** 00-INFRA-0014, 00-INFRA-0015 и 00-INFRA-0017
- **Частично заменяет:** 00-INFRA-0016 только в обязательном совместном размещении worker/provider через UDS и запрете входящего порта выбранного network provider; opaque payload, egress и generic credential mount сохраняются

## Контекст

Любые самостоятельные компоненты инфраструктуры, вычислительные providers и replicas могут находиться на разных физических компьютерах. Ни общий Compose project, ни локальная сеть, Docker daemon, socket или файловый volume не являются гарантированным интерфейсом между ними. Полный локальный режим должен разворачивать тот же состав и проверять те же контракты, что распределённый.

## Ключевые решения

- Каждая самостоятельная роль и реплика получает явное размещение, достижимые endpoints, необходимые identities и собственное состояние. Сеть, hostname, TLS trust и published/advertised ports согласуются до запуска. Операторский endpoint не заменяется внутренним Docker-именем автоматически.
- По явному подтверждению владельца 2026-11-14 SPIRE agent и необходимая доставка собственного удостоверения являются обязательной частью среды каждого узла соответствующей нагрузки; установщик подготавливает их автоматически. Workload API остаётся локальным. Это не требует совместного размещения provider/worker или двух самостоятельных сервисов; server, observer, status publisher и telemetry sender сохраняют собственные placements, private keys и SVID volumes не разделяются между узлами.
- Установщик выполняет публичные владельческие процедуры на целевых узлах и сохраняет receipts. Локальный Docker coordinator остаётся адаптером своего узла, а его успешный запуск не доказывает распределённое развёртывание. Технические SQLite-журналы и SVID volumes принадлежат экземпляру; PostgreSQL, Kafka и другие сетевые capabilities используют собственные сетевые контракты.
- Translator worker и opaque provider имеют независимые network placements. Узкий `POST /v1/translate` и non-billable `GET /healthz` доступны по HTTPS/mTLS, с CA/hostname и exact workload identity. URI provider проверяется до передачи текста. Чужая URI той же CA не является допуском.
- Provider image остаётся неизменяемым black box: без переопределения command, entrypoint, environment и image healthcheck. Generic read-only `transport.json`, identity policy и собственная SVID delivery настраивают только transport. API credential остаётся отдельным provider-only mount. Model ID, upstream endpoint, prompt и execution identity не проходят через opaque-порт или worker configuration.
- `provider` и `worker` устанавливаются отдельными владельческими рецептами на своих узлах. Replicas могут использовать разрешённую контрактом workload identity с отдельными keys/certificates и instance/lease identity. Общий provider/worker volume и межузловой Docker socket не требуются. Прежний bundle остаётся явно ограниченной UDS-совместимостью.
- Рабочий сетевой client проверяет актуальную собственную identity и доверие. Ошибки TLS, конфигурации, транспорта и deadline оставляют задание для прежнего bounded retry; переключение на UDS/HTTP при ошибке запрещено. Provider health не доказывает quota, смысл перевода или продвижение очереди.
- Новая полная локальная и распределённая приёмка использует сетевой профиль. Установка, health, release и допуск provider не выдают `RESUME`, не очищают очередь и не сбрасывают recovery budget.

## Последствия

Размещение перестаёт зависеть от общего хоста, сохраняя непрозрачность реализации и владельцев данных. Возникает обязательный входящий mTLS endpoint provider с собственной областью доверия и admission; прежний security/quality receipt UDS-образа не допускает новый сетевой runtime. Полный инвентарь, автоматизация разных узлов и приёмка replicas остаются проверяемыми требованиями, а не следствием принятия ADR.

## Проверка и откат

Проверяются допустимый opaque запрос, отказ без client certificate, чужая CA/URI, неверный hostname, expiry/withdrawal, обновление SVID, bounded response/deadline и отсутствие передачи текста до аутентификации. Раздельные recipes обязаны работать без общего volume и без запуска второй роли. Повтор установки сохраняет инкарнацию, keys, данные и историю; изменение существующей конфигурации или остановленная/заблокированная цель требует владельческого handoff.

Нужны exact signed provider/worker digests обеих архитектур, SBOM/provenance, quality evidence, clean-room обоих режимов, реальные разные физические узлы и разнесённые replicas. Loopback или несколько сетей одного компьютера учитываются отдельно. Откат возвращает прежние совместимые digests/config после handoff, сохраняя результаты, очередь, сертификаты и pause state.

## Официальные основания

- [Python ssl](https://docs.python.org/3.13/library/ssl.html) — CA/hostname validation и взаимная TLS-аутентификация.
- [Docker Compose services](https://docs.docker.com/reference/compose-file/services/) — независимые recipes, published ports и read-only mounts.
- [SPIFFE Workload API](https://spiffe.io/docs/latest/spiffe-specs/spiffe_workload_api/) — workload identity и доставка собственных удостоверений.
