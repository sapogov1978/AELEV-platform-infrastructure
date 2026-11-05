# Observability — руководство по оркестрации

Браузерный доступ Prometheus и Blackbox Exporter выбирается собственными env владельцев: [local-test/prod Prometheus](prometheus/USER_MANUAL.md#операторский-вход-local-test-и-prod) и [local-test/prod Blackbox](blackbox-exporter/USER_MANUAL.md#операторский-вход-local-test-и-prod). Слой не отключает mTLS и не содержит прокси; он включает те же собственные операторские роли. Прежние технические порты не являются браузерными входами без сертификата.

Текущие проверки самостоятельных поставок и слоя: [результат](test/standalone-validation-result.md).

Слой сохранён как точка подготовки, запуска и остановки всего комплекса. По [ADR-0107](../../../docs/adr/20261011-0107-independent-observability-products.md) процессы, настройки, тома, сети, удостоверения, правила, панели и тесты принадлежат продуктам. Здесь только `include` их канонических Compose, имя группы, выбор имени env и последовательный вызов команд подготовки владельцев. Функциональные потребители неизвестны всему комплексу.

| Самостоятельная поставка | Назначение |
|---|---|
| [delivery](delivery/README.md) | Устойчивый приём телеметрии и подтверждение после фиксации. |
| [OpenTelemetry Collector](otel-collector/README.md) | Стандартная обработка технической телеметрии. |
| [Prometheus](prometheus/USER_MANUAL.md) | Хранение и запросы метрик. |
| [Blackbox Exporter](blackbox-exporter/USER_MANUAL.md) | Защищённые технические пробы. |
| [Loki](loki/README.md) | Хранение и поиск журналов. |
| [Tempo](tempo/README.md) | Хранение и поиск трасс. |
| [Grafana](grafana/README.md) | Панели, запросы и почтовые уведомления. |


## Структура поставки

```text
observability/
├── README.md, SPEC.md, TASKS.md, CHANGELOG.md
├── compose.yml                  # include семи собственных Compose
├── .env.example                 # имя группы и имя включаемого env
├── .env                         # создаётся оператором, вне Git
├── prepare.py                   # вызывает подготовку у владельцев
├── Dockerfile, pyproject.toml, poetry.lock  # проверки слоя, не общий runtime
├── delivery/, otel-collector/, prometheus/, blackbox-exporter/
├── loki/, tempo/, grafana/       # полный состав внутри README владельцев
└── test/                        # проверки композиции наблюдения
```

Это слой композиции, не дополнительный рабочий сервис. В нём нет общего каталога секретов или общего Dockerfile для рабочих процессов; материалы, настройки и данные остаются у владельцев.

## Секреты оркестрации

Этот слой не генерирует общий пароль, не хранит приватную CA и не выдаёт identities вместо владельцев. Перед запуском подготовьте материалы каждого продукта по его разделу «Секреты: получение и подготовка»; [индекс инфраструктуры](../index.md) ведёт прямо к этим разделам. Корневой env не содержит паролей сервисов. Удалённым узлам доставляется только их собственный набор через защищённый механизм, без общей папки и без копирования закрытых ключей CA.

Локальные генераторы служат первоначальной подготовке тестовой PKI, не обновлению production-секретов. Рабочий источник секретов и identity определён ADR-0017: внешнее хранилище, проверенная PKI/SPIRE, выдача по роли и временная доставка. Перед ротацией определите настоящий действующий Compose-проект и обновляйте только роль владельца; общий повтор up или генерации не является сменой пароля, сертификата либо OIDC client-secret. Данные, UUID и journals не удаляются. Незакрытые ограничения файловой/env-доставки указаны у соответствующего владельца, не скрыты общей командой.


## Подготовка и запуск

На операторской машине проверьте `docker version`, `docker compose version`, `python3.13 --version`, `command -v openssl`, `openssl version`. Установка Docker — по [официальной инструкции](https://docs.docker.com/engine/install/); Python 3.13 — поддерживаемый пакет выбранной ОС либо [официальная поставка](https://www.python.org/downloads/). OpenSSL: macOS — `brew install openssl@3`; Debian/Ubuntu — `sudo apt-get update && sudo apt-get install openssl ca-certificates`; Alpine — `sudo apk add openssl ca-certificates`. Автоматической установки или смены ОС нет. После подготовки файлов обычный запуск не требует Python/OpenSSL на host.

Из этого каталога для новой установки:

```sh
python3.13 prepare.py init
# Задайте адреса, доверие и лимиты в .env каждого владельца.
python3.13 prepare.py tls --issuer-dir /secure/local-observability-issuer
python3.13 prepare.py render
docker compose --env-file .env --profile provision --profile ui build
# Перед первым up подготовьте operator-provision Prometheus и Blackbox
# по их собственным README: CA IdP, существующий оператор и файловые секреты.
for task in delivery-configuration-init otel-collector-configuration-init prometheus-configuration-init loki-configuration-init tempo-configuration-init grafana-configuration-init; do
  docker compose --env-file .env --profile provision run --rm "$task" || exit 1
done
docker compose --env-file .env --profile ui up -d --no-build
```

`init` сохраняет существующие env. `tls` — явная локальная PKI испытаний: сертификаты на 30 дней, CA на год; ключи не перезаписываются. Общий явно переданный операторский issuer позволяет выдать совместимые удостоверения; runtime получает только собственные файлы владельца, не ключи CA. Для production доставляется утверждённая PKI/SPIRE с правильными SAN и ротацией по [ADR-0017](../../../docs/adr/20260803-0017-zero-trust-and-operational-security.md). `render` создаёт только объявленные производные файлы владельца, сохраняя отдельно инъектированные панели/цели. Общий публичный wheel технических операций закреплён SHA-256 в собственном lock каждого продукта; общего рабочего образа нет.

Каждая `*-configuration-init` работает без сети/секретов только со своим томом и отказывает при чужих или непустых немаркированных данных. Blackbox Exporter постоянного тома не имеет. Инициализация явная, не заменяет readiness и не пропускается при первой установке. Собственный README каждого продукта содержит полный отдельный запуск из его каталога. Нативные процессы стартуют без профиля; `ui` дополнительно включает независимых издателей. Первичная подготовка их identities выполняется отдельно через `--profile provision run --rm <product>-status-provision`; весь административный профиль через `up` не запускается.

```sh
docker compose --env-file .env --profile ui config --quiet
docker compose --env-file .env --profile ui ps
docker compose --env-file .env --profile ui stop
# Снять контейнеры, сохранив данные:
docker compose --env-file .env --profile ui down
```

Обычная остановка сохраняет данные, identities, UUID и последовательности. Перед переключением самостоятельной/общей группы снимите прежних писателей их `down` без `--volumes`; одновременные писатели запрещены. Старый `observability-configuration-init` заменён шестью задачами владельцев; прежние связанная конфигурация, подготовка и тесты удалены из текущего дерева, их история доступна в Git. Имена постоянных томов и маркеры сохраняются через собственный `DATA_OWNER`, независимый от Compose project.

## Обслуживание слоя

Команды выполняются из каталога observability только для принадлежащего ему проекта. Если продукты уже запущены корневой инфраструктурой, обслуживайте их адресно через корневой Compose; отдельный up этого слоя над теми же томами запрещён. После первичной подготовки обычный повторный запуск — `docker compose --env-file .env --profile ui up -d --no-build --pull never`, без повторной генерации ключей и обнуления identities.

Обновление, согласованная резервная копия и восстановление выполняются по README каждого владельца: слой не имеет собственной общей БД. Сохраняются шесть нативных томов, отдельные журналы издателей, настройки и защищённые удостоверения. Для Prometheus и Blackbox Exporter до запуска обязательна собственная operator-provision; для отключённого UI подготовка publisher identity не нужна. Не переносите операторские клиенты, панели или файлы секретов в этот каталог. [TASKS](TASKS.md) отмечает независимые эксплуатационные допуски; сохранённые отчёты относятся к своим версиям и датам, не к любому последующему up.

## Настройки слоя и географическое размещение

В `.env` слоя ровно два параметра: `AELEV_INFRASTRUCTURE_OBSERVABILITY_COMPOSE_PROJECT` — имя общей группы и `AELEV_INFRASTRUCTURE_OBSERVABILITY_ENV_FILE` — имя включаемого env (`.env` для работы, `.env.example` для проверки шаблонов). Остальные значения только у владельцев. Compose включает те же модели без поправок; верхний `services/infrastructure` включает этот слой целиком.

По умолчанию каждый продукт одноузловой; комплекс можно разместить вместе или разнести по защищённым маршрутизируемым endpoints. Docker bridge и каталог другого сервера через WAN не нужны. `extra_hosts` обслуживает только локальные aliases; для разнесения настройте настоящие DNS/IP, SAN, bind и firewall. На Linux host-loopback недоступен из bridge: задайте конкретный доступный адрес host, не открывайте интерфейсы без сетевой политики. Сбой зависимости может изменить readiness потребителя, но не останавливает его издателя или другие продукты. Потеря UI не останавливает нативные процессы.

Обычная телеметрия использует [устойчивый технический контракт](../../../docs/api/telemetry.md), не доменный Kafka-транспорт. Сборщики, смысл метрик, прикладные правила и панели принадлежат потребителям. Универсальный отправитель, `Dockerfile.sender` и [схема его настройки](delivery/sender.json.example) принадлежат delivery. Grafana отправляет почту только после настройки внешнего SMTP с проверяемым STARTTLS; пока `SMTP_ENABLED=false`, писем нет. Технические проекции и исходный архив различаются; принятый архив не удаляется по возрасту. Единственный диск требует независимой резервной копии и проверенного восстановления.

## Проверки

[Workflow GitHub Actions](https://github.com/sapogov1978/discovery-research-platform/blob/8d1a663319ac15d8729cf1422858f37d57096c75/.github/workflows/audit.yml) выполняет независимые проверки границы, JSON, Markdown-ссылок и синтаксиса Python при `push` и `pull_request`. Сервисные тесты наблюдаемости выполняются отдельно из этого каталога по закреплённым инструкциям; эти проверки не являются запуском реальной инфраструктуры, WAN или полным жизненным циклом. Результат прежнего локального воспроизведения сохранён в [отчёте](test/ci-validation-result.md).

```sh
docker build --target checks --build-context tests=./test -t aelev/observability-checks:local .
docker run --rm --network none aelev/observability-checks:local pytest -p no:cacheprovider test/test_orchestration.py
```

Здесь проверяется только композиция; модульные и реальные жизненные проверки находятся в `test` продуктов. [Спецификация](SPEC.md), [схема](https://github.com/sapogov1978/discovery-research-platform/blob/8d1a663319ac15d8729cf1422858f37d57096c75/docs/architecture/diagrams/infrastructure-observability.mmd). Прежние [результаты доставки](test/delivery-result.md) и [жизненного цикла](test/lifecycle-result.md) исторические и не доказывают новый самостоятельный запуск. Локальная готовность не закрывает WAN, production PKI, нагрузку, внешнюю почту или независимый backup/restore.

После запуска всех продуктов выполните `python3.13 test/check_routes.py`: это чтение настоящих метрик/проб и запросы Grafana через опубликованные защищённые адреса, без создания тестовых сущностей. [Результат маршрутов](test/routes-result.md). Проверка запуска каждого продукта — его `test/startup.py`; она требует свежего события подтверждённой ONLINE-публикации, а не только heartbeat издателя. Полный жизненный цикл в текущем запуске не повторялся.
