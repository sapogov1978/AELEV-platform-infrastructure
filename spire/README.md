# SPIRE

Провайдер удостоверений рабочих нагрузок: один SPIRE Server и отдельные агенты потребителей. Потребитель может закрепить читаемые имена физического Compose-проекта и контейнера агента с обязательным префиксом `aelev_`; точная логическая идентичность остаётся в метках и состоянии SPIRE. Это поддерживаемая одноузловая поставка, не HA-кластер.

## Основные возможности

- Регистрация собственных identities и выдача X.509-SVID.
- Автоматическое обновление и отзыв удостоверений.
- Read-only SVID volume с заранее созданной точкой `identity.json` для отдельной неизменяемой политики потребителя.
- Установка Server и Agent через Shell/PowerShell.
- Зашифрованное резервирование и восстановление.
- Восстановление раздельных UID/GID runtime и установщика; сохранение выбранного native `no` при повторной установке.
- Собственное состояние, телеметрия и панель Grafana.
- Автоматическое 90-дневное сопровождение восьми локальных observation identity с 30-дневным окном, раздельными полномочиями SPIRE и Platform UI и fail-closed readiness.

## Запуск

Сетевой HTTPS источник проверяет и публикует только свежую квитанцию продления своего фактического сертификата. Observer, sender и publisher сопровождают свои сертификаты в собственных локальных томах; общий отчёт на сервере не заменяет готовность независимо размещённых ролей. Local/test prepare создаёт SAN по явным endpoints и пять раздельных renewal requests; первоначальная выдача и source checks не являются production admission либо подтверждением межузлового сопровождения.

Самостоятельные `install.sh/ps1 observer|status-publisher|telemetry-sender` принимают закрытый `component.json` и только свои credential sets. Каждая установка имеет отдельные project/daemon/state и публичные HTTPS endpoints, не требует локального server либо общих проекционных томов. Формат — [руководство](USER_MANUAL.md#самостоятельные-роли-наблюдения); native сценарий четырёх ролей с ONLINE publication ACK, центральной доставкой и повтором без замены контейнеров — [отчёт](../docs/audit/spire-independent-source-20261114.json). Автоматическое межузловое сопровождение и физическая распределённая приёмка остаются отдельными воротами.

Для привязки одной роли выполните на её узле `sh export.sh PROJECT EXPORT_OPERATOR_IMAGE OUTPUT_ROOT server|observer|status-publisher|telemetry-sender` либо PowerShell с `-Component ROLE`. Собственный публичный export содержит только выбранную роль, exact image/container, её external volumes и сохранённую историю рестартов; остальные роли на этом daemon не нужны. Без scope прежний общий четырёхролевой export совместим. Экспорт не запускает контейнеры и не подтверждает readiness; распределённый установщик должен отдельно проверить endpoints и admission.

Первоначальный локальный набор готовит `python3 local-inputs.py --input PRIVATE_CONFIG --output NEW_PRIVATE_ROOT`. SPIRE создаёт собственные query/server/write CA и шесть локальных leaf, принимает два отдельно выданных операторами client identity и токены своей папки Grafana. Чужие issuer keys не входят во входной контракт; три запроса сопровождения готовятся отдельно от фактического enrollment. Формат и последовательность — в [руководстве](USER_MANUAL.md#подготовить-локальный-initial-bundle).

Для полного первоначального комплекта доступен `python3 bootstrap.py validate --initial-input PATH`, затем `python3 bootstrap.py up --project PROJECT --runtime-image IMAGE --installer-image IMAGE --initial-input PATH --output-root PATH`. Комплект содержит `plan.json`, `observation.json` и шесть закрытых наборов из [руководства](USER_MANUAL.md#подключить-наблюдение); bootstrap последовательно вызывает штатные установку сервера, наблюдения и экспорт. `validate` проверяет структуру и права файлов без Docker; native installer проверяет планы и PKI при `up`. Обязательное per-process подтверждение Docker socket сохраняется. Общий full-runtime вызывает этот вход после готовности core.

Выберите [установку сервера](USER_MANUAL.md#установка-сервера) либо [агента](USER_MANUAL.md#регистрация-и-агент). Входы `install.sh` и `install.ps1` принимают собственный проект и закреплённые образы; подготовка и примеры команд находятся в руководстве. Они используют Docker socket только в одноразовой операторской роли с полномочиями, эквивалентными root, и поэтому до каждого вызова требуют явное локальное подтверждение `DRP_SPIRE_DOCKER_SOCKET_APPROVAL=operator-root-equivalent-v1`; постоянные runtime-роли socket не получают. Прикладные права не создаются запуском сервера.

На Linux одноразовый установщик использует `cap-drop ALL` и только `DAC_READ_SEARCH` для чтения собственных явно подключённых read-only входов оператора с другим UID. Он не изменяет host permissions и не получает обхода прав записи. Подробнее — [спецификация](SPEC.md#владение-и-полномочия).

Публичный [экспорт установленного провайдера](USER_MANUAL.md#подключить-установленный-spire-к-общей-композиции) принимает отдельный каталог вывода: необязательный третий аргумент `export.sh` либо `-OutputRoot` у `export.ps1`. Все public binding/Compose/receipt файлы остаются одним комплектом; прежний `.env` владельца не затрагивается. Полный установщик учитывает действующий физический координатор SPIRE без переноса его контейнеров.

Для объявленных локальных маршрутов наблюдения на Docker Desktop общий Python-оператор Shell/PowerShell разрешает только `host.docker.internal` отдельным read-only helper от UID 10001 без Docker socket, секретов и host mounts. Подтверждённый private IPv4 попадает в `extra_hosts` собственных runtime-рецептов; оператор с socket сохраняет `network=none`. Без объявленных aliases resolver не запускается.

## Навигация

- [architecture.mmd](architecture.mmd) — диаграмма границы, компонентов и основных потоков владельца.
- [USER_MANUAL.md](USER_MANUAL.md) — установка, регистрация, отзыв и восстановление.
- [SPEC.md](SPEC.md), [ENDPOINTS.md](ENDPOINTS.md) — правила и технические интерфейсы.
- [src/](src), [test/](test) — реализация и проверки.
- [INDEX.md](INDEX.md), [TASKS.md](TASKS.md), [CHANGELOG.md](CHANGELOG.md) — агентская карта, ограничения и история.
