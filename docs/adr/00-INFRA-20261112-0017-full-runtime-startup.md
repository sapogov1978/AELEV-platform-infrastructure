# 00-INFRA-20261112-0017-full-runtime-startup — Полный штатный запуск инфраструктуры

- **Статус:** Принят
- **Дата:** 2026-11-12
- **Основание:** аксиомы 3, 5, 6, 7; 00-INFRA-20261106-0010, 00-INFRA-20261106-0015, 00-INFRA-20261109-0016

## Контекст

Capability bootstrap запускает только часть инфраструктуры. Полный состав определён в `full-local.json`, но его запуск был доступен через приёмочные сценарии с тестовыми входами. Потребителю нужен штатный вход для того же состава с сохранением владельческих credentials, данных и состояний обработки.

## Ключевые решения

- Публичный `bootstrap.py --full-runtime` предоставляет `plan`, `init`, `build`, `up`, `status`, `down`. Он не совмещается с `--capability`; прежний capability CLI сохраняется.
- Состав, профили, images и зависимости берутся из существующего manifest и владельческих Compose. Вторые service definitions не создаются.
- Штатная подготовка не выбирает health-only fixture переводчика и сохраняет предоставленные владельческие credentials. Provider-specific параметры и секреты не печатаются.
- До старта проверяются installation owner, service configuration hash, native image ID и число реплик. Drift требует owner handoff. `--no-recreate` сохраняет существующие инкарнации и их recovery budgets.
- SPIRE подключается только через полный публичный экспорт владельца с согласованной immutable generation и receipt текущих container/image IDs, native restart history и физического координатора. Его четыре роли остаются в этом проекте, исключены из корневого `up` и повторно учитываются в общей готовности. Устаревший/неполный экспорт, смешанные координаторы или второй писатель запрещают общий start/status/stop. Отдельный output root экспорта сохраняет прежний ручной `.env`. Корень не создаёт второй provider из шаблонов, не переносит установку и не восстанавливает остановленные native-роли неявно.
- Одноразовая подготовка создаёт только собственные ресурсы инфраструктуры. Полный старт сохраняет migration barriers и проверяет все объявленные роли.
- Новая установка принимает закрытый owner initial bundle через `--spire-initial-input`: после готовности core вызывается публичный bootstrap SPIRE, который делегирует native server/observation/export. Привязка сохраняется в корневом env атомарно; повтор использует действующую установку без повторной выдачи полномочий. Повреждённый прежний экспорт не становится основанием создать новую установку.
- В local/test вместо полного bundle можно передать `--spire-local-input`: два выданных отдельными операторами client identity, планы и доверие поступают явно, собственную PKI готовит SPIRE. После готовности core недостающие scoped Grafana tokens выдаёт её публичный OIDC-оператор; временная identity снимается после установки. Незавершённое операторское намерение блокирует повтор выдачи, внешние issuer keys и положительные renewal reports этим путём не создаются. Фактические enrollment, доставка и проверка сопровождения остаются обязательными до readiness/admission.
- Команды не выполняют `RESUME`, не выдают quality admission и не удаляют volumes. Неизвестный результат команды требует сверки без автоматического повтора.
- Полный локальный core запускается по владельцам из source-pinned deployment catalog, в топологическом порядке существующих Compose dependencies. Внутри владельца сохраняется канонический совместный `up` всех ещё неготовых ролей: quorum peers не превращаются в последовательность «первый healthy до запуска остальных». Следующий владелец ожидает готовности предыдущего; здоровая группа только наблюдается. До первого start проверяются catalog recipe hashes, замкнутость role dependencies и отсутствие service/owner cycles. Это порядок локального control plane, не требование физической колокации распределённого продукта, не новый backend и не изменение endpoint/healthcheck/recovery.
- `RUNTIME_HEALTHY` подтверждает технические контракты состава. Clean-room evidence, реальный inference и допуск площадки остаются отдельными доказательствами.

## Последствия

Потребитель получает полный штатный запуск через опубликованный интерфейс. Повторный `up` запускает согласованную поставку; обновление несовпадающей инкарнации остаётся отдельным handoff.

## Проверка и откат

Проверяются полнота manifest, изоляция fixture, разделение build/start, отказ при drift и неизвестном результате, сохранение реплик и реальные clean-room старты через публичный вход. `down --full-runtime` сохраняет данные. Возврат к прежнему выпуску требует handoff и не удаляет историю новой операции.

Перед уточнением owner startup объявлены 45–60 минут code/проверок, затем отдельный full gate. Scope исследован по настоящему resolved model: 53 core-роли, 40 одновременно независимых стартов, 14 owners, замкнутые dependencies без owner cycles. В отдельном Engine увеличение 6→8 GiB не устранило health timeouts; sampler не обнаружил новых memory-limit событий, но зафиксировал CPU pressure. Ограничение startup burst проверяется как candidate, а не объявленная причина всех отказов. Quorum и конечные readiness/командные deadlines сохраняются.

## Официальные основания

- [Docker Compose up](https://docs.docker.com/reference/cli/docker/compose/up/) определяет `--no-recreate` для сохранения существующих контейнеров.
- [Docker Compose config](https://docs.docker.com/reference/cli/docker/compose/config/) предоставляет канонический Compose model и service configuration hash.
- [Compose startup order](https://docs.docker.com/compose/how-tos/startup-order/) различает running, healthy и completed-successfully conditions; [Python TopologicalSorter](https://docs.python.org/3.13/library/graphlib.html) предоставляет используемое упорядочение. Неизвестные predecessors отвергаются до sorter.
- [etcd quorum](https://etcd.io/docs/v3.5/faq/#what-is-failure-tolerance) требует большинства членов; конкретное owner grouping остаётся продуктовым решением сохранения канонического bootstrap, а не предписанием документации о числе параллельных контейнеров.
