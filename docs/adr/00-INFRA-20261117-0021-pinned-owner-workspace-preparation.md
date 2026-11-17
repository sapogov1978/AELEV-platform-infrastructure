# 00-INFRA-20261117-0021-pinned-owner-workspace-preparation — Подготовка отдельного каталога владельца

- **Статус:** Принят как реализация подтверждённого плана автоматической подготовки по 00-INFRA-0018
- **Дата:** 2026-11-17
- **Уточняет:** 00-INFRA-0019 и 00-INFRA-0020; текущий runtime и admission не меняются

## Контекст

Owner-compose требует уже подготовленный каталог на Docker daemon host. Source generation остаётся неизменяемой; private env и публичные bind-mounted файлы должны быть доставлены отдельно перед его вызовом.

## Ключевые решения

- Каждый экземпляр получает свой явный workspace на целевом узле; service/replica не зависят от совместного размещения или общего filesystem.
- Полная сверка source/input hashes предшествует созданию нового каталога; повтор существующего допускает только проверку без перезаписи.

Отдельная procedure `owner-prepare` подготавливает один private workspace на выбранном local/SSH узле. Она использует закреплённые executor/owner manifests, catalog role и явно доставленный private env. Публичные файлы копируются только из catalog owner prefix, закрытые/неперечисленные файлы source checkout не импортируются. Runtime/replica получают отдельные явно выбранные workspaces, без общего межузлового диска.

Workspace находится вне обеих source generations. Новый каталог публикуется после полной hash/mode проверки, fsync файлов/каталогов и rename в пределах одного filesystem; существующий не перезаписывается. Advisory lock сериализует helper writers. Повтор сверяет public source snapshot, exact env bytes и receipt; drift требует отдельного handoff. `status` только читает подготовленный каталог и не создаёт отсутствующий. Node journal по-прежнему различает historical success и fresh observation.

Private root/receipt/env имеют `0700/0600`; вложенные public directories/files — `0755/0644` с сохранением executable bit. Это разрешает unprivileged workloads читать свой bind-mounted public config, без расширения прав закрытого env. Новые owner-managed runtime/secrets outputs не становятся source inputs и не сканируются/не восстанавливаются этим helper. Хеши подготовленных public files/env проверяются перед использованием owner-compose; каталог не является canonical domain storage.

Уточнение локального ещё не опубликованного SDK: optional `private_files` добавляет явно выбранные bootstrap inputs под `secrets/`. Их source files остаются `0600` и входят в outer input hashes; target допускает только выбранный владельцем `0600/0640` с UID/primary GID target operator. Это необходимо для непривилегированных owner roles: Compose file-secret bind не применяет `uid/gid/mode` remapping. Максимум 127 дополнительных файлов по 1 MiB сохраняет предел node-artifact 128 вместе с env. Пересечение public/private paths, traversal, чужой режим, symlink и drift закрыто блокируются. Bytes/modes/GID входят в private receipt и общий workspace hash; публичная projection не расширяется. Пустой набор сохраняет прежние env-only receipts. Owner lifecycle outputs и dynamic SVID volumes не превращаются в immutable bootstrap inputs.

## Последствия

Helper не выбирает endpoints, не переписывает env, не выпускает CA/credentials/grants, не обращается к чужому canonical storage, Docker или OpenAI, не запускает приложение и не выполняет RESUME. PKI и остальные операции остаются у собственных владельцев. Typed result содержит только status и hashes workspace/public files/environment. Raw env, source tree, пути и диагностика не публикуются.

## Проверка и откат

Проверяются exact bytes/modes, source/input drift, чужой/отсутствующий receipt, повтор без перезаписи, read-only status, параллельный writer и сбой до публикации. Native qualification должна связать подготовку настоящего владельца с resolved owner-compose plan. Старые node procedures/receipts сохраняются; прежние scopes не переписываются новым executor. Приёмка нового SDK release и всего inventory выполняется отдельно.

Перед блоком объявлен прогноз 90–120 минут для SDK, DRP-связки и bounded qualification; отдельные regressions — 15–20 минут. Это не изменение принятого стека или отказ от независимого размещения.

Перед уточнением private files объявлены 45–60 минут SDK и 30–45 минут controller integration. Scope предварительно сопоставлен с input transport limits, target filesystem permissions, env-only replay и owner certificate lifecycle; endpoints и процедуры выдачи PKI не меняются.

## Официальные основания

- [Docker bind mounts](https://docs.docker.com/engine/storage/bind-mounts/) привязывает файлы к **daemon host**; поэтому inputs доставляются и готовятся на каждом назначенном узле.
- [Compose environment](https://docs.docker.com/compose/how-tos/environment-variables/) объясняет роль env в interpolation; helper сохраняет операторские bytes вместо замены адресов.
- [Python filesystem operations](https://docs.python.org/3.13/library/os.html#os.rename), [fsync](https://docs.python.org/3.13/library/os.html#os.fsync) и [advisory flock](https://docs.python.org/3.13/library/fcntl.html#fcntl.flock) задают используемые механизмы, а create-only/handoff политику определяет продукт.
- [OWASP logging exclusions](https://cheatsheetseries.owasp.org/cheatsheets/Logging_Cheat_Sheet.html#data-to-exclude) обосновывает hash-only public projection закрытых inputs.
- [Compose file-secret permissions](https://docs.docker.com/reference/compose-file/services/#secrets) и [service-specific secrets](https://docs.docker.com/compose/how-tos/use-secrets/) определяют выбранную файловую доставку и необходимость фактических target modes.
