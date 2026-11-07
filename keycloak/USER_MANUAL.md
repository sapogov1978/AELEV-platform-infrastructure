# Keycloak — руководство пользователя

## Структура поставки

```text
keycloak/
├── README.md, SPEC.md, TASKS.md, CHANGELOG.md
├── compose.yml                   # Keycloak, его БД, издатель и provision
├── .env.example                  # шаблон; .env — рабочие настройки вне Git
├── Dockerfile                    # оптимизированный образ Keycloak
├── Dockerfile.status             # собственный издатель и проверки
├── keycloak-entrypoint.sh        # файловые секреты в память процесса
├── recovery.py                   # полный backup/restore владельца
├── realm.json                    # минимальный realm, без каталога потребителей
├── themes/aelev/login/           # наследуемая тема и восстановление формы
├── pyproject.toml, poetry.lock
├── status_publisher.py
├── secrets/
│   ├── README.md                 # описание TLS-материала
│   ├── tls.crt, tls.key          # отдельная доставка, вне Git/образа
│   ├── admin-truststore.p12      # необязательный ручной CLI, не runtime
│   └── status/                   # собственные TLS/CA и OIDC client-secret
└── test/                         # проверки и читаемые результаты
```

Дерево показывает значимые файлы поставки, а не кэши инструментов. `.env`, содержимое `secrets/` и производные `runtime/` создаются оператором и не входят в Git или образ; документация `secrets/README.md`, где она есть, остаётся в Git. Постоянные данные находятся в именованных Docker volumes из Compose, не в каталоге исходников.

## Назначение


Самостоятельный одноузловой поставщик идентичности. Одна поставка содержит production-режим Keycloak, принадлежащий ему PostgreSQL, обязательный TLS, собственную сеть, постоянный volume базы и минимальный realm без OIDC-клиентов конкретных потребителей. Отдельной «будущей production-конфигурации» нет: тот же Compose запускается на любом узле после задания адреса, сертификата и секретов этого узла.

Последние проверки штатной отписки и возврата: [результат](test/presence-validation-result.md), [настоящая браузерная проверка](test/status-publication-result.md). Они не подменяют прежнее испытание полного жизненного цикла БД и сервера.

Собственный издатель имеет `stop_grace_period: 2m`: это время на завершение текущего запроса и подтверждение отписки через удалённый HTTPS, не запасное значение env и не бесконечное ожидание. Если связь недоступна, неподтверждённый выход сохраняется в журнале, а UI не скрывает строку как штатно завершённую.

Издатель состояния подтверждает периодические публикации и при SIGTERM/SIGINT отправляет штатную отписку по [каноническому контракту присутствия](../docs/api/platform-ui-injection.md#присутствие-и-штатная-отписка): строка исчезает после подтверждения и следующего опроса UI. Без отписки строка остаётся с «Нет связи» после срока свежести. Возврат сохраняет UUID, ревизию и последовательность собственного журнала. Повторные неподтверждённые запросы сохраняют точные байты; отписка не удаляет БД, identities или секреты.

## Восстановление давно открытой формы входа

Нормативный порядок предварительного обновления определён в [SPEC](SPEC.md#восстановление-формы-входа). Он устраняет потерю первой отправки пароля при истёкшей попытке Keycloak без увеличения сроков сессий. Действие применяется только к обычной форме имени и пароля; MFA и другие шаги сохраняют штатное поведение.

Проверка потери всех cookie и восстановления исходного OIDC-запроса: [результат от 26 октября](test/login-cookie-recovery-result-20261026.md).

При обновлении существующей установки соберите новые образы из этой поставки и задайте их уникальные теги в собственных `AELEV_INFRASTRUCTURE_KEYCLOAK_IMAGE` и `AELEV_INFRASTRUCTURE_KEYCLOAK_UI_IMAGE`. Затем адресно обновите только Keycloak и выполните собственное deployment-действие выбора темы:

```sh
docker compose build keycloak keycloak-status-provision
docker compose up -d --no-deps --no-build --pull never keycloak
docker compose --profile provision run --rm --no-deps keycloak-status-provision login-theme
```

Выполните последнее действие только после готовности HTTPS/discovery Keycloak. Оно читает собственные административный secret file и CA из прежних mounts и обновляет только `loginTheme`, не клиентов издателя или приложений. Общий штатный `provision` также выбирает тему. Новая установка получает выбор при импорте `realm.json`; повторный импорт не обновляет уже существующий realm. Перезапуск одноузлового IdP кратковременно прерывает его HTTP-доступность, но не требует перезапуска базы или рабочих сервисов. Уже открытая старая форма получает исправление после перезагрузки страницы.

При потере cookie восстановления форма автоматически начинает новую штатную OIDC-попытку с исходными параметрами приложения, проверяет новую форму и отправляет пароль один раз. Чтобы это работало и после неверного пароля, адрес авторизации сохраняется только в хранилище своей вкладки, с привязкой к серверному контексту клиента; пароль там не сохраняется. При запрете хранилища восстановление остаётся доступным на исходной странице авторизации, но после перехода может потребоваться заново открыть приложение. Повторный отказ не создаёт бесконечного цикла.

Если предварительная проверка недоступна из-за соединения, форма не отправляет пароль в старую попытку: восстановите соединение и нажмите «Войти» ещё раз. Если изменился шаг или контекст авторизации, откройте приложение заново. Пароль никуда не копируется; отсутствие связи не выдаётся за неверные credentials. Для отключения исправления выберите `keycloak.v2` в настройке Login theme собственного realm; сроки, пользователи и клиенты не изменяются. Возврат прежнего образа допустим только при совместимости с действующей файловой доставкой секретов: нельзя возвращать образ без её поддержки поверх нового env. Нестандартная существующая тема требует отдельного согласования, действие её закрыто отклоняет.

### Проверки формы

Основные проверки поставки: `poetry run pytest`, `poetry run ruff check .`, `poetry run black --check .` и `poetry run mypy status_publisher.py recovery.py`. Браузерные сценарии используют отдельно подготовленный Playwright с Chromium; при отсутствии пакета в стандартном поиске задайте `AELEV_INFRASTRUCTURE_KEYCLOAK_TEST_PLAYWRIGHT_MODULE` как путь к установленному модулю. В рабочий образ Node.js и браузер не добавляются.

```sh
node --test test/login-refresh.test.cjs
AELEV_INFRASTRUCTURE_KEYCLOAK_TEST_IMAGE=<проверяемый-образ> node test/login-expiry.cjs
```

Вторая команда создаёт одноразовые оптимизированный Keycloak и PostgreSQL 17.6 исключительно для проверки с синтетическим пользователем, отдельным realm и сроком попытки 15 секунд. Файл импорта строится из того же имени realm по обязательному шаблону `<realm>-realm.json`. Тестовая сеть изолирована от рабочих сетей; публикуется случайный HTTP-порт только на loopback. Для одного тестового узла выбран `cache=local`, JVM heap ограничен 512 МиБ внутри контейнера 1 ГиБ/2 CPU; рабочие IdP, кэш, лимиты, таймауты и credentials не используются и не меняются.

При исчерпании стандартного пула Docker можно передать `AELEV_INFRASTRUCTURE_KEYCLOAK_TEST_SUBNET` с заранее проверенной свободной подсетью только одноразового теста; рабочие сети не переопределяются. Дополнительно проверяются потеря всех cookie авторизации и их потеря после неверного пароля: восстановление сохраняет исходный state и успешный обмен кода с S256 PKCE, без повторной автоматической отправки credentials.

На первичную инициализацию пустой тестовой БД отводится до 900 секунд; `AELEV_INFRASTRUCTURE_KEYCLOAK_TEST_STARTUP_TIMEOUT_SECONDS` при необходимости задаёт от 30 до 1800 секунд только тестового ожидания, не сроков входа или сессий. Ранний выход контейнера обнаруживается до конца ожидания с кодом завершения, признаком OOM и журналом после скрытия синтетических паролей. Проверка воспроизводит прежние две отправки с отключённым исправлением, затем доказывает одну отправку с исправлением и успешный обмен кода с S256 PKCE; неверный пароль остаётся ошибкой. Потеря cookie моделируется на уже открытой странице после штатной начальной проверки hash сессии; проверка Keycloak не отключается.

По умолчанию собственные контейнеры, сеть и временные файлы удаляются в `finally`, чужие ресурсы не затрагиваются. Только для локальной диагностики `AELEV_INFRASTRUCTURE_KEYCLOAK_TEST_RETAIN_FAILURE=1` сохраняет неуспешный синтетический стенд и выводит его точные имена и каталог; это не успешный результат проверки. После диагностики оператор обязан проверить label `aelev.test=keycloak-login-recovery` и удалить только эти одноразовые ресурсы и их сгенерированные файлы. Это проверка жизненного цикла входа, не замена обязательного production HTTPS, многорепличного кэша или внешних PKI/WAN-допусков.

## Секреты: получение и подготовка

### Перенос прежних паролей из env без ротации

Если существующий `.env` ещё содержит `AELEV_INFRASTRUCTURE_KEYCLOAK_ADMIN_PASSWORD` и `AELEV_INFRASTRUCTURE_KEYCLOAK_DATABASE_PASSWORD`, используйте собственный инструмент миграции. Он интерпретирует значения штатным Compose, не выводит их, проверяет отдельные файлы внутри собственного `secrets`, сохраняет посторонние настройки и атомарно заменяет env. Новые пароли не создаются; несовпадение существующего файла, ссылка, повтор переменной или неоднозначное многострочное значение закрыто останавливают операцию. Незавершённый перенос можно повторить: уже записанные совпадающие файлы сохраняются.

```sh
python3.13 migrate_password_files.py
python3.13 migrate_password_files.py --apply
docker compose config --quiet
```

Первая команда только проверяет план. В env остаются пути `ADMIN_PASSWORD_FILE`, `DATABASE_PASSWORD_FILE` и группа `SECRET_GID` с префиксом владельца; файлы имеют права `0640`, env — `0600`. Инструмент фиксирует данные и записи каталогов до удаления копий значений из env; он не меняет учётную запись администратора или пароль SQL-роли. Для удаления прежних парольных значений из конфигурации уже созданных контейнеров требуется адресное пересоздание Keycloak и его БД на прежнем томе; это прерывает вход на время старта IdP.

Перед переключением проверьте точное имя действующего Compose-проекта и соответствие тома. Согласуйте локальный `COMPOSE_PROJECT` с действующей установкой, не переименовывая её; для установки, ранее запущенной корневым Compose, можно явно передать то же имя через `-p` вместе с собственным `-f compose.yml`. Сначала остановите только Keycloak при ещё работающей БД, чтобы штатно снять старого участника кэша; затем пересоздайте только его БД, дождитесь её готовности и запустите Keycloak. Не создавайте параллельный проект и не используйте `--remove-orphans` или удаление томов. Остальные роли корневого проекта не затрагиваются. Смена пароля не выполняется ни переносом файла, ни таким пересозданием.

| Материал | Источник и использование |
|---|---|
| `secrets/https/tls.key`, `tls.crt`, `ca.crt` | Ключ создаёт оператор, CSR передаёт выбранной CA; сертификат должен соответствовать PUBLIC_URL и используемому ADMIN_ENDPOINT, а `ca.crt` содержит публичное overlap-доверие. Точные команды находятся в разделе [генерации TLS](#генерация-tls-ключа-и-сертификата). |
| `secrets/admin-password` | Уникальный пароль bootstrap-администратора из внешнего хранилища; на пустой БД создаёт пользователя, далее provision требует действующий пароль, не новую случайную строку. |
| `secrets/database-password` | Отдельный пароль собственной SQL-роли из внешнего хранилища; не совпадает с административным паролем. |
| `secrets/admin-truststore.p12` | Необязательный ручной CLI: создаётся keytool из публичной CA по разделу TLS. Пароль этого файла хранит оператор отдельно; он не нужен контейнерам. |

На локальном стенде два исходных пароля можно сгенерировать без вывода в терминал непосредственно в игнорируемые Git файлы, которые читает Compose:

```sh
umask 077
mkdir -p secrets/https
test ! -e secrets/admin-password || exit 1
test ! -e secrets/database-password || exit 1
openssl rand -out secrets/admin-password -hex 32
openssl rand -out secrets/database-password -hex 32
KEYCLOAK_SECRET_GID="$(id -g)"
chgrp "$KEYCLOAK_SECRET_GID" secrets/admin-password secrets/database-password
chmod 640 secrets/admin-password secrets/database-password
chmod 600 .env
```

Числовой `KEYCLOAK_SECRET_GID` должен совпадать с `AELEV_INFRASTRUCTURE_KEYCLOAK_SECRET_GID` в `.env`: контейнеры получают эту дополнительную группу и только групповое чтение файлов. В `.env` находятся только пути `AELEV_INFRASTRUCTURE_KEYCLOAK_ADMIN_PASSWORD_FILE` и `AELEV_INFRASTRUCTURE_KEYCLOAK_DATABASE_PASSWORD_FILE`, а не значения. PostgreSQL использует `POSTGRES_PASSWORD_FILE`; entrypoint Keycloak проверяет отдельные обычные файлы и экспортирует значения только внутри памяти запускаемого процесса, поэтому Docker Compose/inspect их не хранит. Одноразовый provisioner получает только административный файл. В production эти же пути указывают на доставленные внешним secret manager временные файлы с group-read для настроенного runtime GID; локальная схема не является обходом.

Не повторяйте генерацию ключей поверх существующих `https/tls.key`/`https/tls.crt`: для новой установки сначала убедитесь, что файлов нет; для ротации создайте отдельный набор. Закреплённый образ Keycloak работает с UID 1000 и дополнительной группой `AELEV_INFRASTRUCTURE_KEYCLOAK_SECRET_GID`. После доставки назначьте private key этой группе и режим `0640`; read-only mount не меняет права автоматически. Каталог должен допускать проход, сертификат может иметь `0644`. Изменение файлов паролей после инициализации не обновляет пользователей или SQL-роли. SQL-пароль меняют согласованно в существующей БД и подключении Keycloak; административный — в Keycloak и файле одноразового provision. При смене TLS используйте проверенный сертификат и контролируемый рестарт собственного Keycloak, без смены issuer или удаления базы.

### Секреты издателя состояния

Пути ниже соответствуют шаблону `UI_SECRET_ROOT=./secrets/status`; при другом значении используйте собственный настроенный каталог. Источники доверия определяются адресами, не совместным размещением: узел в другой стране получает публичные цепочки CA и своё удостоверение через защищённую доставку, а не доступ к чужой папке.

| Файл | Откуда получить и зачем |
|---|---|
| `shell-ca.crt` | Проверенная публичная цепочка CA HTTPS-сервера оболочки; получить у её оператора. Это не CA клиентских издателей. |
| `identity-ca.crt` | Проверенная публичная цепочка CA HTTPS IdP; получить у оператора IdP. |
| `publisher.crt`, `publisher.key` | Собственное удостоверение `clientAuth`; его выпускающая CA должна входить в `publisher-ca.crt` приёмника UI. Закрытый ключ принадлежит только этому издателю. |
| `client-secret` | Выдаёт Keycloak для собственного confidential client; сохраняет `keycloak-status-provision`. Не генерируйте произвольный пароль вместо секрета существующего OIDC-клиента. |
| HTTPS-пробы Keycloak | Используют `identity-ca.crt` и SAN адреса Keycloak; дополнительный SQL-пароль или клиентский сертификат пробе не нужен. |
Собственный `keycloak-status-provision` получает `ADMIN_USER` и отдельный read-only `admin-password`; значение не входит в environment. Отдельный `status-admin` в этой поставке не используется.

Перед локальной подписью проверьте `openssl version` и поддержку OpenSSL 3; при отсутствии используйте утверждённый пакет вашей ОС (`brew install openssl@3` на macOS, `sudo apt-get install openssl` на поддерживаемой Debian/Ubuntu) или подготовленную операторскую станцию. Эти команды не устанавливают CA в системное доверие и не выполняются при запуске контейнера.


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

Тестовую CA издателей создаёт оператор локальной оболочки при её явной подготовке PKI; берутся именно `issuer/publisher.crt/key`, не серверный ключ UI. В рабочей среде последнее подписание локальной CA не выполняют: удостоверение и обновляемую identity доставляет утверждённая PKI/SPIRE по [00-INFRA-20261106-0007-workload-and-operator-identity](../docs/adr/00-INFRA-20261106-0007-workload-and-operator-identity.md). CSR можно передать CA, закрытый ключ нельзя. Перед включением издателя доставьте также перечисленные файлы нативных проб и выполните собственный `--profile provision run --rm keycloak-status-provision` из раздела запуска.

### Хранение, доступ и ротация

Секреты разделяются по среде, владельцу и роли. Локальные файлы — только материал стенда или доставка, не каноническое production-хранилище; рабочий источник — внешнее утверждённое хранилище, доставка в память/tmpfs по 00-INFRA-20261106-0007-workload-and-operator-identity. Не включайте секреты в Git, образ, архив исходников, команды с подстановкой содержимого или отчёты. Не запускайте `set -x`, не публикуйте раскрытый `docker compose config`; проверяйте через `config --quiet`. Резервные копии секретов хранятся отдельно, зашифрованно, с ограниченным доступом; закрытые CA — только у оператора/KMS/HSM, никогда у рабочего процесса.

Для status-файлов установите каталоги `0750`, файлы `0640`, группу из собственного `UI_SECRET_GID`; владельцем каталога должен быть оператор с `UI_PROVISION_UID`, которому разрешена запись client-secret. UID рабочего издателя остаётся заданным собственным образом/Compose; дополнительная группа даёт только чтение. В командном примере ниже STATUS_SECRET_GID — числовое значение уже настроенного UI_SECRET_GID, не новая переменная сервисного env. Не делайте ключи общедоступными ради запуска. Права нативных секретов описаны отдельно выше.

```sh
: "${STATUS_SECRET_GID:?set the numeric GID from your UI_SECRET_GID}"
chgrp "$STATUS_SECRET_GID" secrets/status secrets/status/*
chmod 750 secrets/status
chmod 640 secrets/status/*
```

Административный пароль находится в отдельном файле `secrets/admin-password`; `UI_PROVISION_UID` должен иметь право только его чтения и записи `client-secret` в каталог status. Keycloak получает тот же secret file отдельным read-only mount; копия `status-admin` не используется.

Проверьте срок, SAN/EKU и цепочку сертификата до запуска: `openssl x509 -in secrets/status/publisher.crt -noout -dates -ext extendedKeyUsage`, `openssl verify -CAfile /secure/ui-publisher-ca.crt -purpose sslclient secrets/status/publisher.crt`. Локальное удостоверение из команды действует 30 дней и не обновляется автоматически. Для смены CA сначала доставляют доверие старой и новой цепочкам, затем новые удостоверения, проверяют соединения и только после этого отзывают старые. Ротацию проводят через отдельный подготовленный набор файлов и контролируемое пересоздание собственной роли; генераторы первичного запуска не используют для перезаписи активных ключей. Новый client-secret сначала создают в собственном клиенте Keycloak, затем доставляют повторным собственным provision и пересоздают издателя; обычный повтор provision сам по себе не является ротацией. UUID, identity, ревизия и журнал последовательностей при этом сохраняются.


## Подготовка и самостоятельный запуск

Все команды без другого указания выполняются из этого каталога. Если уже работает общая группа инфраструктуры, управляйте её ролями из [корня](../README.md): самостоятельная группа не должна одновременно писать в те же тома. Проверьте Docker и Compose; шаблон копируется только при отсутствии рабочего `.env`. `config --quiet` проверяет модель без вывода раскрытых паролей.

```sh
docker version
docker compose version
if [ ! -f .env ]; then
  (umask 077; cp .env.example .env)
fi
chmod 600 .env
# Заполните секреты и публичный URL, затем положите TLS-файлы в ./secrets.
docker compose --env-file .env config --quiet
docker compose --env-file .env build keycloak
docker compose --env-file .env up -d keycloak-database keycloak
```

`compose.yml` не содержит запасных значений публичных настроек компонента. Отсутствующая или пустая обязательная переменная останавливает `docker compose config` с указанием отсутствующего ключа. `.env.example` является шаблоном, а рабочие значения находятся только в игнорируемом `.env` с правами `0600` либо поступают из защищённого механизма среды выполнения. Секреты нельзя коммитить, выводить в журналы или повторно использовать.

Сертификат должен быть действителен для host из `AELEV_INFRASTRUCTURE_KEYCLOAK_PUBLIC_URL`. Если собственная задача `keycloak-status-provision` обращается по внутреннему имени `keycloak`, это имя также должно находиться в SAN сертификата. Обязательные файлы `tls.crt`, `tls.key` и публичный `ca.crt` размещаются в минимальном каталоге [`secrets/https`](secrets/README.md) рядом с Compose; собственный издатель и его задача используют PEM CA из `UI_SECRET_ROOT`. Compose монтирует каталог целиком, чтобы атомарная замена leaf не сохраняла старый inode. Проверка TLS не отключается. Все mounts запрещают Docker создавать отсутствующий путь на узле: без TLS-каталога или realm постоянный Keycloak не создаётся, без каталога собственных удостоверений не запускаются роли публикации состояния. `admin-truststore.p12` больше не нужен поставке: старый универсальный шаблон без скрипта удалён по [00-INFRA-20261106-0004-self-contained-service-delivery](../docs/adr/00-INFRA-20261106-0004-self-contained-service-delivery.md). Уже созданный файл сохраняется для возможного ручного административного CLI и не заменяет PEM CA издателя.

## Проверка и установка OpenSSL и keytool

Команды генерации выполняются на станции оператора до запуска Compose. В runtime Dockerfile Keycloak эти инструменты не устанавливаются: private key не должен попадать в слой образа, build cache или registry. Для TLS нужен OpenSSL с поддержкой `req -addext`. `keytool` из JDK нужен только при необязательном создании PKCS12 для ручного административного CLI, не для старта или собственной задачи настройки. Ниже показана проверка обоих инструментов; если ручной PKCS12 не используется, проверку и установку JDK пропустите.

Перед генерацией выполните функциональную проверку. Проверки только пути через `command -v keytool` недостаточно: macOS может содержать `/usr/bin/keytool` как неработающую заглушку без установленной Java.

```sh
missing=0

if ! openssl version >/dev/null 2>&1; then
  echo "OpenSSL is not installed or cannot start" >&2
  missing=1
elif ! openssl req -help 2>&1 | grep -q -- "-addext"; then
  echo "OpenSSL does not support req -addext; install OpenSSL 1.1.1 or newer" >&2
  missing=1
fi

if ! keytool -help >/dev/null 2>&1; then
  echo "keytool is not installed or no working JDK is available" >&2
  missing=1
fi

if [ "${missing}" -ne 0 ]; then
  exit 1
fi

openssl version
keytool -J-version
```

На macOS с Homebrew установите OpenSSL 3 и OpenJDK 21, затем добавьте их исполняемые файлы в `PATH` текущей сессии. Формулы `openssl@3` и `openjdk@21` являются keg-only, поэтому нельзя полагаться на системные `/usr/bin/openssl` и `/usr/bin/keytool`.

```sh
brew install openssl@3 openjdk@21
export PATH="$(brew --prefix openssl@3)/bin:$(brew --prefix openjdk@21)/bin:${PATH}"
```

Официальные описания пакетов: [Homebrew OpenSSL 3](https://formulae.brew.sh/formula/openssl@3) и [Homebrew OpenJDK 21](https://formulae.brew.sh/formula/openjdk@21).

На Debian или Ubuntu установите CLI OpenSSL и headless JDK из системного репозитория.

```sh
sudo apt-get update
sudo apt-get install --no-install-recommends openssl default-jdk-headless
```

Состав пакетов подтверждён каталогом Debian: [`openssl`](https://packages.debian.org/openssl) содержит CLI `/usr/bin/openssl`, а [`default-jdk-headless`](https://packages.debian.org/stable/default-jdk-headless) устанавливает стандартный headless JDK.

На Fedora установите OpenSSL и OpenJDK 21 Development Environment.

```sh
sudo dnf install openssl java-21-openjdk-devel
```

Пакеты перечислены в официальном каталоге Fedora: [`openssl`](https://packages.fedoraproject.org/pkgs/openssl/openssl/) и [`java-21-openjdk-devel`](https://packages.fedoraproject.org/pkgs/java-21-openjdk/). Для другой ОС необходимо установить пакеты, предоставляющие рабочие команды `openssl` и `keytool`, после чего повторить функциональную проверку выше.

## Генерация TLS-ключа и сертификата

Для production сначала создайте private key и CSR. В `KEYCLOAK_PUBLIC_HOST` укажите DNS-имя из `AELEV_INFRASTRUCTURE_KEYCLOAK_PUBLIC_URL`. Для сертификата публичного CA административный endpoint должен использовать то же публичное имя: внутреннее имя `keycloak` публичный CA не подпишет.

```sh
umask 077
mkdir -p secrets/https
KEYCLOAK_SECRET_GID="${KEYCLOAK_SECRET_GID:-$(id -g)}"
KEYCLOAK_PUBLIC_HOST=identity.example.com
test ! -e secrets/https/tls.key && test ! -e secrets/https/tls.crt || exit 1
openssl genpkey \
  -algorithm RSA \
  -pkeyopt rsa_keygen_bits:3072 \
  -out secrets/https/tls.key
openssl req \
  -new \
  -sha256 \
  -key secrets/https/tls.key \
  -out secrets/https/tls.csr \
  -subj "/CN=${KEYCLOAK_PUBLIC_HOST}" \
  -addext "subjectAltName=DNS:${KEYCLOAK_PUBLIC_HOST}"
chgrp "$KEYCLOAK_SECRET_GID" secrets/https/tls.key
chmod 640 secrets/https/tls.key
chmod 600 secrets/https/tls.csr
```

Передайте `secrets/https/tls.csr` доверенному центру сертификации и сохраните возвращённый сертификат вместе с необходимой цепочкой как `secrets/https/tls.crt`, затем задайте ему права `0644`. Публичную цепочку issuer сохраните как `secrets/https/ca.crt`; при смене CA сначала включите старую и новую цепочки. Если публичный URL использует IP-адрес и выбранный CA подписывает IP-сертификаты, SAN задаётся в виде `IP:203.0.113.10`, а не `DNS:203.0.113.10`. Внутреннее имя `keycloak` добавляется только в сертификат частного CA, когда административный endpoint действительно использует `https://keycloak:8443`.

```sh
chmod 644 secrets/https/tls.crt secrets/https/ca.crt
```

Для автономной локальной проверки можно создать self-signed сертификат. Он не является production-сертификатом и потребует явного доверия на каждой клиентской машине.

```sh
umask 077
mkdir -p secrets/https
KEYCLOAK_SECRET_GID="${KEYCLOAK_SECRET_GID:-$(id -g)}"
KEYCLOAK_LOCAL_IP=127.0.0.1
test ! -e secrets/https/tls.key && test ! -e secrets/https/tls.crt || exit 1
openssl req \
  -x509 \
  -newkey rsa:3072 \
  -sha256 \
  -days 365 \
  -nodes \
  -keyout secrets/https/tls.key \
  -out secrets/https/tls.crt \
  -subj "/CN=${KEYCLOAK_LOCAL_IP}" \
  -addext "subjectAltName=IP:${KEYCLOAK_LOCAL_IP},DNS:keycloak"
chgrp "$KEYCLOAK_SECRET_GID" secrets/https/tls.key
chmod 640 secrets/https/tls.key
chmod 644 secrets/https/tls.crt
cp secrets/https/tls.crt secrets/https/ca.crt
chmod 644 secrets/https/ca.crt
```

Необязательно: для ручного `kcadm.sh` можно создать PKCS12 truststore. Для CA-signed сертификата `KEYCLOAK_TRUST_CERTIFICATE` указывает на доверенный issuing/root CA, для локального self-signed — на `secrets/https/tls.crt`. Пароль вводится только для этой операторской команды и не является переменной env сервиса. Не перезаписывайте существующий файл; штатные контейнеры его не читают.

```bash
KEYCLOAK_TRUST_CERTIFICATE=/path/to/issuing-ca.crt
test ! -e secrets/admin-truststore.p12 || exit 1
read -r -s -p "Keycloak truststore password: " KEYCLOAK_TRUSTSTORE_PASSWORD
echo
export KEYCLOAK_TRUSTSTORE_PASSWORD
keytool \
  -importcert \
  -noprompt \
  -storetype PKCS12 \
  -alias aelev-keycloak-ca \
  -file "${KEYCLOAK_TRUST_CERTIFICATE}" \
  -keystore secrets/admin-truststore.p12 \
  -storepass:env KEYCLOAK_TRUSTSTORE_PASSWORD
unset KEYCLOAK_TRUSTSTORE_PASSWORD
chmod 600 secrets/admin-truststore.p12
```

Проверить SAN, срок действия и соответствие сертификата private key можно до запуска контейнера. Два последних SHA-256 значения должны совпасть.

```sh
openssl x509 -in secrets/https/tls.crt -noout -subject -issuer -dates -ext subjectAltName
openssl x509 -in secrets/https/tls.crt -pubkey -noout | openssl pkey -pubin -outform DER | openssl sha256
openssl pkey -in secrets/https/tls.key -pubout -outform DER | openssl sha256
keytool -list -v -keystore secrets/admin-truststore.p12
```

## Параметры `.env`

| Переменная | Значение шаблона | Назначение и влияние |
|---|---|---|
| `AELEV_INFRASTRUCTURE_KEYCLOAK_COMPOSE_PROJECT` | `drp-infrastructure-keycloak` | Имя проекта Docker Compose. Изолирует контейнеры и служебные ресурсы этой установки. |
| `AELEV_INFRASTRUCTURE_KEYCLOAK_IMAGE` | `aelev/keycloak:26.3.2-production` | Имя локально собираемого оптимизированного образа Keycloak. Версию исходного образа закрепляет Dockerfile. |
| `AELEV_INFRASTRUCTURE_KEYCLOAK_DATABASE_IMAGE` | `postgres:17.6-alpine` | Точная версия выделенного PostgreSQL. Это хранилище принадлежит только Keycloak и не публикуется другим сервисам. |
| `AELEV_INFRASTRUCTURE_KEYCLOAK_ADMIN_USER` | `admin` | Имя bootstrap-администратора, создаваемого только при первой инициализации пустой базы. Изменение после инициализации не переименовывает существующую учётную запись. |
| `AELEV_INFRASTRUCTURE_KEYCLOAK_ADMIN_PASSWORD_FILE` | `./secrets/admin-password` | Путь к секрету bootstrap-администратора и одноразовой задачи конфигурации. Изменение файла после первой инициализации не меняет пароль существующей учётной записи. |
| `AELEV_INFRASTRUCTURE_KEYCLOAK_DATABASE_NAME` | `keycloak` | Имя единственной базы, в которой Keycloak хранит realms, пользователей, clients, роли, ключи и служебное состояние. |
| `AELEV_INFRASTRUCTURE_KEYCLOAK_DATABASE_USER` | `keycloak` | Владелец базы Keycloak. Учётная запись не используется другими компонентами. |
| `AELEV_INFRASTRUCTURE_KEYCLOAK_DATABASE_PASSWORD_FILE` | `./secrets/database-password` | Путь к отдельному случайному паролю роли PostgreSQL. Изменение файла требует согласованно изменить пароль в существующей базе. |
| `AELEV_INFRASTRUCTURE_KEYCLOAK_BIND` | `0.0.0.0` | Адрес интерфейса узла, на котором публикуется HTTPS. Для ограниченного интерфейса указывается его точный IP; это не меняет публичный issuer. |
| `AELEV_INFRASTRUCTURE_KEYCLOAK_HTTPS_PORT` | `8443` | Порт узла, перенаправляемый на HTTPS-порт `8443` контейнера. Он должен совпадать с портом в публичном URL, если используется не стандартный `443`. |
| `AELEV_INFRASTRUCTURE_KEYCLOAK_PUBLIC_URL` | `https://identity.example.com:8443` | Канонический внешний URL Keycloak. Из него формируются OIDC issuer, discovery metadata и redirect-ссылки; потребители должны использовать `${PUBLIC_URL}/realms/aelev`. |
| `AELEV_INFRASTRUCTURE_KEYCLOAK_ADMIN_ENDPOINT` | `https://identity.example.com:8443` | HTTPS endpoint собственной задачи keycloak-status-provision. Для сертификата публичного CA используется публичное имя; внутреннее имя допустимо только при наличии соответствующего SAN и доверенного частного CA. |
| `AELEV_INFRASTRUCTURE_KEYCLOAK_DATABASE_VOLUME` | `drp-infrastructure-keycloak-database` | Именованный volume PostgreSQL. Изменение имени подключает новую пустую базу; штатные `stop` и `down` данные не удаляют. |
| `AELEV_INFRASTRUCTURE_KEYCLOAK_NETWORK` | `drp-infrastructure-keycloak` | Закрытая сеть только для Keycloak, его PostgreSQL и одноразового provisioner. PostgreSQL не имеет host-порта. |
| `AELEV_INFRASTRUCTURE_KEYCLOAK_NETWORK_SUBNET` | `10.254.80.0/28` | Явная непересекающаяся подсеть локальной закрытой сети; предотвращает неуправляемый выбор исчерпанного Docker address pool. |
| `AELEV_INFRASTRUCTURE_KEYCLOAK_INGRESS_NETWORK` | `drp-infrastructure-keycloak-ingress` | Сеть публикации HTTPS и исходящего соединения собственного издателя. PostgreSQL и административная задача в неё не входят; без неё Docker не публикует порт контейнера, подключённого только к закрытой `internal` сети. |
| `AELEV_INFRASTRUCTURE_KEYCLOAK_INGRESS_NETWORK_SUBNET` | `10.254.80.16/28` | Явная непересекающаяся подсеть локальной ingress-сети; перед установкой оператор проверяет отсутствие маршрута и Docker-сети с тем же CIDR. |
| `AELEV_INFRASTRUCTURE_KEYCLOAK_MEMORY_LIMIT` | `2g` | Жёсткий предел памяти Keycloak. Java heap рассчитывается как доля этого предела; изменение требует проверки реальной нагрузки и отсутствия OOM. |
| `AELEV_INFRASTRUCTURE_KEYCLOAK_DATABASE_MEMORY_LIMIT` | `1g` | Жёсткий предел памяти выделенного PostgreSQL. Он ограничивает влияние БД на узел и одновременно задаёт верхнюю границу доступного ей cache. |

## Фиксированные параметры Compose и Dockerfile

| Параметр | Почему необходим |
|---|---|
| `KC_DB=postgres` и оптимизированная сборка | Исключают непригодную для production встроенную `dev-file` БД и заранее собирают Keycloak для выбранного драйвера. |
| `command: ["start", "--optimized", "--import-realm"]` | Запускает production-режим с уже собранной конфигурацией и импортирует минимальный realm только при его отсутствии. `start-dev` не используется. |
| `KC_HTTP_ENABLED=false` | Не допускает открытый HTTP endpoint аутентификации. Потребители обращаются только по HTTPS. |
| `KC_HTTPS_CERTIFICATE_FILE` и `KC_HTTPS_CERTIFICATE_KEY_FILE` | Подключают явно смонтированные TLS certificate и private key; запуск без них завершается ошибкой. |
| Относительные mounts `./realm.json` и `./secrets/*` | Делают каталог переносимой единицей: файлы разрешаются относительно Compose компонента, а корневая оркестрация меняет только префикс расположения каталога. Пути рабочей станции в `.env` отсутствуют. |
| `KC_HOSTNAME` | Фиксирует доверенный внешний origin и issuer, не позволяя входящему `Host` незаметно изменить публикуемые OIDC URL. |
| `KC_HEALTH_ENABLED=true` | Включает readiness endpoint на непубликуемом management-порту `9000` для внешнего оркестратора и наблюдаемости. В Keycloak `26.3.2` он наследует TLS основного endpoint. |
| `KC_METRICS_ENABLED=true` | Включает технические метрики на непубликуемом management interface; их можно читать только из явно подключённой служебной сети. |
| TCP healthcheck порта `8443` | Закреплённый Keycloak `26.3.2` не содержит более позднюю настройку `http-management-scheme`, а минимальный официальный образ не содержит HTTP-клиента. В этой версии TLS-порт открывается после завершения начальной инициализации; после Docker healthcheck жизненный тест дополнительно проверяет OIDC discovery по HTTPS и выполнение административной задачи. Порт `9000` не публикуется на узле. |
| `POSTGRES_INITDB_ARGS=--auth-host=scram-sha-256 --data-checksums` | При создании базы включает SCRAM для сетевой парольной аутентификации и контрольные суммы страниц. На уже созданный volume параметр повторно не действует. |
| `depends_on: service_healthy` | Не запускает Keycloak до успешной проверки PostgreSQL, а издатель и provisioner — до открытия TLS-порта Keycloak. Полную HTTPS readiness отдельно проверяет издатель; TCP healthcheck её не заменяет. |
| `restart: unless-stopped` | Возвращает оба постоянных процесса после сбоя или рестарта Docker, сохраняя явную остановку оператором. Provisioner остаётся одноразовым с `restart: "no"`. |
| Закрытая backend-сеть и отдельная ingress-сеть | PostgreSQL и `keycloak-status-provision` используют только `internal: true` сеть. Keycloak и собственный издатель дополнительно подключены к ingress-сети для публичного HTTPS и отправки состояния. |

Минимальный [`realm.json`](realm.json) создаёт включённый realm `aelev`, требует TLS, запрещает самостоятельную регистрацию и вход по email. Он намеренно не содержит пользователей и OIDC-клиентов конкретных сервисов. Их clients, роли, учётные записи и задачи настройки принадлежат поставкам самих потребителей, не корню инфраструктуры или Keycloak.

Собственная одноразовая задача `keycloak-status-provision` имеет профиль `provision` и выполняется через `run --rm` по разделу публикации ниже. Она настраивает только identity собственного издателя; повтор идемпотентен, административный контейнер после завершения удаляется. Имена и зависимости уже заданы в сервисном Compose и одинаковы в самостоятельном и общем запуске. Старый `configuration-applier` и его неиспользуемая переменная `AELEV_INFRASTRUCTURE_KEYCLOAK_ADMIN_TRUSTSTORE_PASSWORD` удалены; удалите эту строку из прежнего рабочего env. Ключи, truststore, база, клиенты и пользователи этой миграцией не удаляются.

## Проверка конфигурации

Python-проверка принадлежит этой поставке и находится в [`test/test_configuration.py`](test/test_configuration.py). Она проверяет единое пространство `AELEV_INFRASTRUCTURE_KEYCLOAK_*`, отказ при отсутствии `.env`, production-команду, TLS, собственный PostgreSQL, обязательные файловые mounts, профиль одноразовой настройки и политику перезапуска. Проверка требует Python 3.13 и Docker Compose, выполняет только `docker compose config`, не запускает контейнеры и не изменяет Docker-ресурсы.

```sh
python3 test/test_configuration.py
```

## Проверка жизненного цикла

Собственный Python-тест использует этот `compose.yml`, рабочий env, подготовленные образы и TLS с проверкой CA/имени. Фиктивных IdP, OIDC-клиентов, образов или init нет. Поднимаются Keycloak и собственная БД, выполняется настоящий `keycloak-status-provision`, запускается издатель; проверяются healthcheck и HTTPS discovery. Проверки stop/up и down/up подтверждают сохранность issuer и ID собственного машинного клиента, повтор provision — идемпотентность. Для этого заранее подготовьте удостоверения и собственную роль издателя по разделу ниже. Тест не требует доступного приёмника UI: его доставка отдельно проверяется браузерным тестом.

Условия: контейнеров этой группы и других работающих писателей её томов нет; все образы заранее собраны. Существующие данные допускаются только с явным `--reuse-existing-data`, без разрешения на их удаление. Тест фиксирует Docker-ресурсы и в `finally` снимает созданные контейнеры/сети и только новые доказанно принадлежащие ему тома; исходные ресурсы, секреты и образы сохраняются. Выполняйте в согласованное остановочное окно.

```sh
python3.13 test/lifecycle.py --reuse-existing-data
```

Текущий читаемый результат — [`test/lifecycle-result.md`](test/lifecycle-result.md). Старые shell-проверка, её фиктивная конфигурация и прежний отчёт перенесены в исторический архив; они не являются актуальным рецептом. WAN, резервное восстановление, высокая доступность и продукционная PKI этим локальным тестом не подтверждаются.

## Публикация в состоянии платформы

Ревизия 4 объявляет кнопку «Открыть» в группе и строке Keycloak: PUBLIC_URL собственного env с путём /admin/ ведёт на существующую административную консоль. Это доступный браузеру канонический адрес, не внутренний ADMIN_ENDPOINT издателя. Пользователь проходит собственную авторизацию консоли; токен оболочки и полномочия издателя не дают административных прав. База и издатель ссылку не наследуют. При изменении PUBLIC_URL требуется новая ревизия описания по [контракту](../docs/api/platform-ui-injection.md#ссылки-на-собственные-интерфейсы), UUID/identity/последовательность сохраняются.

[Результат проверки ссылки](test/open-link-result.md): собственные регрессии и открытие настоящей консоли в браузере.

Повторная проверка названия назначения и технологического состава: [результат](test/status-labels-result.md). Она не подменяет прежние испытания жизненного цикла.

Собственная декларация содержит раскрываемую группу «Идентификация и доступ» (`Identity and access`) с тремя строками: `Keycloak`, `Keycloak Database`, `Keycloak Status Publisher`. Верхний уровень отражает назначение, состав — продукты и процессы; названия публикует владелец, не таблица переименований оболочки. Строка процесса проверяется через HTTPS `/health/live`, БД — по отдельному результату `Keycloak database connections async health check` в `/health/ready`; это состояние соединения Keycloak со своей БД, не проверка резервного копирования или всей PostgreSQL. Такой источник описан в [официальной документации Keycloak](https://www.keycloak.org/observability/health) и проверен на закреплённом `26.3.2`. При отсутствующей, неоднозначной или непонятной проверке БД показывается `UNKNOWN`, а не копия статуса процесса. Издатель сообщает о выполнении текущего цикла с устойчиво записанным отчётом; оболочка видит его `ONLINE` только после приёма, а после прекращения сообщений — `UNKNOWN` по TTL. Ни Docker socket, ни подключение издателя к PostgreSQL не требуются.

Общее состояние требует положительных liveness, readiness и проверки соединения с БД; любой подтверждённый `DOWN` означает `OFFLINE`, недостаток сведений — `UNKNOWN`. Состав появился в ревизии 2, название назначения — в ревизии 3. Для установки с поддержкой состава достаточно обновить только образ издателя; UUID, identity, том и последовательность сохраняются. Неподтверждённый отчёт повторяется без изменения байтов. Правила добавочного поля и отката определены только в [контракте](../docs/api/platform-ui-injection.md#составные-компоненты).

`keycloak-status-publisher` — собственный технический процесс этой поставки, не новый инфраструктурный продукт. Он проверяет HTTPS `/health/ready` и публикует название назначения, состав и состояние через JWT/mTLS по [контракту оболочки](../docs/api/platform-ui-injection.md). Никакого поиска Docker-контейнеров нет. `INFRASTRUCTURE` без функциональных разделов появляется в «Состоянии», не в левом меню. Управление контейнерами или встраивание административной панели этим изменением не добавляются.

Официальный образ Keycloak не содержит такого издателя, поэтому для него используется собственный Python 3.13 из принятого стека. Сборка `Dockerfile.status` использует только этот каталог и собственные `pyproject.toml`/`poetry.lock`. У рабочего процесса нет пароля администратора, Docker socket или доступа к чужим БД. Его том хранит только UUID, последовательность и неподтверждённый технический отчёт. Отказ оболочки или издателя не останавливает Keycloak и его PostgreSQL; при отсутствии публикаций без отписки строка остаётся с «Нет связи», просроченное наблюдение становится `UNKNOWN`. Проверка здоровья контейнера издателя подтверждает продвижение цикла, не доставку недоступной оболочке. Правила — в [SPEC](SPEC.md).

Перед первым запуском перенесите новые `UI_*` из `.env.example` в свой рабочий env. Для разных установок создайте разные UUID один раз; сохраните UUID, identity и том при переносе. SAN должен подтверждать имя из `UI_HEALTH_URL`; с публичным CA без внутреннего SAN задайте доступное издателю доверенное имя сервера. Настройки содержат только собственную зависимость от технического интерфейса представления, не перечень потребителей IdP.

| Суффикс `AELEV_INFRASTRUCTURE_KEYCLOAK_` | Назначение |
|---|---|
| `UI_IMAGE` / `UI_PYTHON_IMAGE` | Собственный образ издателя и закреплённый Python 3.13; стек Keycloak не заменяется. |
| `UI_URL` | Доступный издателю HTTPS-адрес оболочки, в том числе удалённой. |
| `UI_REALM` / `UI_CLIENT_ID` | Realm и отдельный машинный клиент собственного издателя. |
| `UI_AUDIENCE` | Точное значение `KEYCLOAK_CLIENT_ID` оболочки для проверки access token. |
| `UI_MODULE_ID` | Постоянный UUID установки, не меняется после рестарта. |
| `UI_HEALTH_URL` | Собственная HTTPS management `/health/ready`; адрес `/health/live` получается из того же пути заменой последнего сегмента. TLS, host, порт и необязательный префикс пути не меняются. Это не TCP-проба или проверка чужого сервиса. |
| `UI_INTERVAL_SECONDS` / `UI_TIMEOUT_SECONDS` | Пауза после цикла и предел запроса; период меньше TTL оболочки с запасом. |
| `UI_STATE_VOLUME` | Собственный том последовательности и точного неподтверждённого запроса; не удаляйте его отдельно от identity. |
| `UI_SECRET_ROOT` | Собственный относительный каталог файлов доверия, удостоверения и секрета клиента. |
| `UI_SECRET_GID` / `UI_PROVISION_UID` | Группа чтения runtime и UID записи одноразовой задачи; задайте свои `id -g`/`id -u`. |
| `UI_MEMORY_LIMIT` | Отдельный предел памяти издателя. |

`ADMIN_ENDPOINT` используется для выдачи токена и одноразовой настройки своей identity. `ADMIN_USER` и файловый административный секрет доступны только Keycloak и этой задаче; в рабочем издателе их нет. Задача настраивает точный audience mapper и только необходимую роль `platform-ui-publisher`. Роль просмотра и административные роли рабочему процессу не выдаются. Чужой одноимённый клиент не перезаписывается, повтор собственной настройки не пересоздаёт субъект или секрет.

Доставьте из утверждённой PKI в `UI_SECRET_ROOT` публичные `identity-ca.crt` и `shell-ca.crt`, отдельные `publisher.crt`/`publisher.key` с назначением `clientAuth`. Сертификат издателя должен быть подписан CA, которому доверяет `publisher-ca.crt` оболочки. Закрытые ключи CA в runtime не доставляются. Каталог принадлежит `UI_PROVISION_UID`, группа совпадает с `UI_SECRET_GID`; права каталога `0750`, файлов `0640`. Одноразовая задача создаст здесь `client-secret`; runtime читает каталог только через read-only mount.

Только для локальной установки с уже созданной тестовой CA оболочки допускается выпуск отдельного издателя на станции оператора. Указанные пути — явные параметры оператора, не пути к соседним исходникам в поставке. Не перезаписывайте существующие ключи. Проверьте OpenSSL по разделу выше; для `-copy_extensions copy` этих команд требуется OpenSSL 3:

```sh
umask 077
mkdir -p secrets/status
KEYCLOAK_STATUS_CA_CERT=/path/to/local-publisher-ca.crt
KEYCLOAK_STATUS_CA_KEY=/path/to/local-publisher-ca.key
KEYCLOAK_STATUS_IDENTITY_CA=/path/to/identity-ca.crt
KEYCLOAK_STATUS_SHELL_CA=/path/to/shell-ca.crt
cp "$KEYCLOAK_STATUS_IDENTITY_CA" secrets/status/identity-ca.crt
cp "$KEYCLOAK_STATUS_SHELL_CA" secrets/status/shell-ca.crt
openssl req -new -newkey rsa:3072 -nodes \
  -keyout secrets/status/publisher.key -out secrets/status/publisher.csr \
  -subj /CN=keycloak-status \
  -addext basicConstraints=critical,CA:FALSE \
  -addext extendedKeyUsage=clientAuth \
  -addext keyUsage=critical,digitalSignature,keyEncipherment
openssl x509 -req -days 30 -copy_extensions copy \
  -in secrets/status/publisher.csr \
  -CA "$KEYCLOAK_STATUS_CA_CERT" -CAkey "$KEYCLOAK_STATUS_CA_KEY" \
  -set_serial "0x$(openssl rand -hex 16)" -out secrets/status/publisher.crt
chmod 750 secrets/status
chmod 640 secrets/status/*
```

После собственной настройки оболочки, которая создаёт роль издателя, выполните из каталога Keycloak:

```sh
docker compose --env-file .env build keycloak-status-publisher
docker compose --env-file .env --profile provision run --rm keycloak-status-provision
docker compose --env-file .env up -d --no-deps keycloak-status-publisher
docker compose --env-file .env ps keycloak-status-publisher
```

При новой установке сначала запускаются Keycloak и его БД по начальному разделу README. После явного provision обычный `docker compose up -d` также запускает издателя. Корневой Compose подключает ту же поставку. Соединение с оболочкой идёт по её адресу, не через общую Docker-сеть; `host.docker.internal:host-gateway` поддерживает только явное локальное размещение, удалённому URL не требуется.

Изолированные проверки — [test/test_status_publisher.py](test/test_status_publisher.py). Проверка реальной регистрации и браузерного отображения — [test/status-publication-result.md](test/status-publication-result.md). По умолчанию она не останавливает Keycloak/БД; явный `--component-lifecycle` проверяет stop/start каждой собственной части. Фиктивные модули и identity не создаются.

Собственный образ проверок собирается из этого каталога. Значение build-arg ниже берётся из `UI_PYTHON_IMAGE` вашего env, не является вторым источником настройки:

```sh
set -a
. ./.env
set +a
docker build --target checks \
  --build-arg "PYTHON_IMAGE=$AELEV_INFRASTRUCTURE_KEYCLOAK_UI_PYTHON_IMAGE" \
  -f Dockerfile.status -t aelev/keycloak-status-checks:local .
docker run --rm aelev/keycloak-status-checks:local pytest -q
docker run --rm aelev/keycloak-status-checks:local black --check status_publisher.py recovery.py test
docker run --rm aelev/keycloak-status-checks:local ruff check .
docker run --rm aelev/keycloak-status-checks:local mypy status_publisher.py recovery.py
```

Для браузерного теста нужен отдельный временный venv с `playwright==1.56.0` и Chromium, как у проверок оболочки. Запускайте [test/status_publication.py](test/status_publication.py) из своего каталога; все адреса и файл пароля оператора передаются явно, не импортируются из чужой поставки. Тест входит реальным viewer, проверяет собственную группу и три отдельных состояния, раскрытие/сворачивание щелчком и клавиатурой, смену языка, сохранение раскрытия при повторном настоящем запросе API, границы JWT/mTLS и при явном флаге перезапускает только издателя. Минутная пауза браузерного опроса проверяется управляемыми часами Chromium; источник состояния и ответ сервера не подменяются:

```sh
/path/to/browser-venv/bin/python test/status_publication.py \
  --ui-url https://platform.example.com:8443 \
  --viewer-user operator \
  --viewer-password-file /path/to/viewer-password \
  --chromium /path/to/chromium \
  --restart-publisher
```

Для проверки настоящих остановок добавьте `--component-lifecycle --status-ttl-seconds <STATUS_TTL_SECONDS вашей оболочки>` вместо `--restart-publisher`. До запуска все три контейнера этой поставки должны работать; тест откажется менять неполную установку. Каждый компонент останавливается отдельно и запускается в `finally`, даже если ожидаемый статус не получен. Тома, сети, UUID и identity сохраняются; посторонние контейнеры и оболочка не останавливаются. Эти действия временно нарушают вход и соединение с БД: запускайте проверку только в согласованное окно. Минутный браузерный опрос ускоряется часами Chromium, но срок свежести и состояние проверяются настоящим сервером и реальным временем, без подмены ответов. Между отказами браузер получает новый короткоживущий JWT через настоящий OIDC-вход; проверка подписи, сроков и ролей не отключается. Ускорение таймеров не отправляет время JWT в будущее.

При остановке самого Keycloak его строка получает `UNKNOWN`: удалённый издатель не может доказать, что именно произошло — остановка процесса, недоступность сети или ошибка TLS. При работающем Keycloak и остановке собственной БД её readiness-проверка подтверждает `DOWN`, и строка получает `OFFLINE`. При штатной остановке издатель подтверждает отписку, и строка исчезает; без отписки после срока свежести строка остаётся с «Нет связи». Возврат каждого компонента должен восстановить `ONLINE`; отсутствие связи нельзя представлять как доказанный отказ или сохранять зелёным бессрочно. HTTPS-адреса и доверие задаются владельцем, общая Docker-сеть с оболочкой не требуется; локальная проверка через маршрутизируемый адрес не доказывает работу настоящего WAN, DNS и внешней PKI.

## Перенос на другой узел

Для новой установки переносятся этот каталог, закрытые настройки, TLS-материал и проверенная резервная копия PostgreSQL, если должны сохраниться пользователи, clients, роли и ключи realm. Работающий каталог PostgreSQL нельзя копировать как обычные файлы: используйте согласованную резервную копию и отдельную проверку восстановления. Сохраняются также журнал, UUID и identity издателя. Затем меняются `BIND`, `HTTPS_PORT`, `PUBLIC_URL` и сертификат, после чего выполняются те же команды запуска. Проверьте OIDC discovery и реальный вход до переключения потребителей. Потребители получают новый issuer/JWKS endpoint через собственные настройки и перезапускаются. Если меняется `PUBLIC_URL`, ранее выпущенные токены содержат старый issuer и должны быть перевыпущены; если база не перенесена, новая установка является новым пустым доменом идентичности.

Эта поставка одноузловая: Keycloak и его PostgreSQL находятся в одном failure domain и не обеспечивают высокой доступности. Это ограничение топологии, а не признак dev-конфигурации: runtime уже использует production-команду, TLS и поддерживаемую внешнюю относительно процесса Keycloak БД.

## Остановка, обновление и резервная копия

У самостоятельной установки `docker compose --env-file .env stop` сохраняет контейнеры и данные, `down` снимает контейнеры и сети без удаления именованных томов. После подготовки издателя повторный `up -d --no-build --pull never` запускает все три постоянные роли. `down --volumes` не является очисткой кэша: оно удаляет БД с пользователями и ключами, а также журнал последовательности издателя.

Для полной копии используйте штатный инструмент владельца. Он получает логический custom-format dump PostgreSQL, согласованную через SQLite backup API копию журнала издателя и точные realm/TLS/OIDC/парольные файлы, затем записывает размеры и SHA-256 каждого файла. Каталог содержит секреты в открытом виде с правами `0600`, поэтому его необходимо создавать непосредственно на зашифрованном носителе вне области отказа узла:

```sh
python3.13 recovery.py backup /encrypted/backup/keycloak-2026-10-14
python3.13 recovery.py verify /encrypted/backup/keycloak-2026-10-14
```

Для восстановления подготовьте отдельный каталог поставки с теми же версиями, новым именем Compose-проекта, новыми пустыми именованными volumes и путями к ещё не существующим secret files. Realm artifact в Git должен точно совпадать с manifest. Все целевые контейнеры должны быть остановлены; инструмент откажется перезаписывать любой файл, непустую БД, существующий журнал, другой UUID или повреждённый bundle:

```sh
python3.13 recovery.py restore /encrypted/backup/keycloak-2026-10-14
python3.13 recovery.py verify /encrypted/backup/keycloak-2026-10-14
```

Для изолированной проверки данных без подключения издателя к рабочей Platform UI добавьте `--data-only` к `restore` и `verify`. Этот режим всё равно поднимает новую PostgreSQL и Keycloak, проверяет восстановленные realm и журнал издателя, но не запускает сам издатель; он предназначен для rehearsal, а не для штатного запуска восстановленной установки. Фактический прогон в новые volumes и проверка OIDC discovery сохранены в [отчёте](test/recovery-result.md).

После технической проверки обязательно проверьте OIDC discovery, вход сохранённого реального пользователя, получение и обновление token, единственность собственного клиента издателя и только затем переключайте потребителей. Успешный dump или container health сам по себе этого не доказывает.

Перед обновлением Keycloak подготовьте проверенную копию, точный прежний образ и настройки, проверьте миграцию на отдельной восстановленной установке. Изменение содержимого файлов паролей не меняет пароль уже существующей учётной записи или SQL-роли автоматически. Откат образа допустим только при совместимости со схемой; после несовместимой миграции требуется проверенное восстановление соответствующей копии, не запуск старого Keycloak поверх новой схемы.

## Обновление журнала издателя

Сборка собственного издателя использует закреплённую SQLite 3.53.4; ОС, Python и SDK не заменяются. До первого запуска задайте `AELEV_INFRASTRUCTURE_KEYCLOAK_UI_RESTART_POLICY=no`. При обновлении сначала удержите точного предшественника через [существующий ограничитель](../USER_MANUAL.md#ограниченное-восстановление-по-unhealthy), остановите только издателя и сохраните полный его том с WAL, inspect и журналы. Не вызывайте отписку, не удаляйте том и не меняйте UUID.

На изолированных копиях проверьте целостность, схему и все строки старым и новым движками. Запускайте только проверенный издатель с прежними mounts и `restart=no`, без запуска зависимостей. После healthy передайте прежний бюджет штатным `replace`; только для действительно незарегистрированной истории с подтверждёнными нулевыми рестартами допустим `enroll`. Значение env оставьте `no`: фактический оставшийся бюджет выставляет ограничитель, а новая инкарнация должна пройти передачу истории снова. Исчерпанный или заблокированный бюджет не сбрасывайте. Перед следующей заменой повторяйте эту процедуру, а не обычный пересоздающий `up`.

## Лёгкая проба издателя

При обновлении сохраняйте фактическое имя Compose-проекта работающего издателя и передавайте его явно через `-p`. Имя самостоятельной группы из примера не подставляется вместо действующей общей группы: два издателя одного журнала одновременно запрещены.

Соберите собственный совместимый образ с закреплённым клиентом Platform UI 1.1.1 и локальным `publisher_health_probe.py`, сохраняющим прежние путь и порог. Закрепите его в `AELEV_INFRASTRUCTURE_KEYCLOAK_UI_IMAGE` и проверьте Compose. До адресного обновления только `keycloak-status-publisher` сохраните согласованную SQLite-копию собственного журнала и конфигурацию; не удаляйте том, UUID, sequence или ожидающие байты. Одной замены команды healthcheck без совместимого образа недостаточно. Применяйте `up -d --no-deps --no-build --pull never` только к издателю с профилем `ui`, затем проверьте свежий heartbeat, продвижение sequence и доставку состояния. Нативные процессы, данные, секреты и бюджеты восстановления не меняются. Точные свойства пробы определены в [SPEC](SPEC.md); её успех не заменяет проверки готовности и доставки.

При откате остановите только новый издатель и выберите сохранённый совместимый образ с прежней командой пробы, не меняя журнал. Схема журнала этим обновлением не меняется; возврат более старого клиента с несовместимым форматом без отдельной подготовки не допускается. Если процесс не восстановился, сохраняйте журналы и безопасную причину ошибки, не обнуляйте последовательность и не увеличивайте таймаут вместо устранения причины.

## Официальные руководства

- [Running Keycloak in a container](https://www.keycloak.org/server/containers) и [production configuration](https://www.keycloak.org/server/configuration-production) — production mode, образ, TLS и внешняя база.
- [Keycloak health checks](https://www.keycloak.org/observability/health) — штатные health/readiness endpoints.
- [Importing and exporting realms](https://www.keycloak.org/server/importExport) — поддерживаемые границы переноса realm.
- [Keycloak securing applications with OIDC](https://www.keycloak.org/securing-apps/oidc-layers) — discovery, authorization code flow и проверка токенов.
