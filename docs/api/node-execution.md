# Выполнение владельческой процедуры на узле, версия 1

`tools/node_execution.py execute|status --binding PRIVATE_JSON --request PRIVATE_JSON` вызывает закреплённую публичную процедуру владельца локально либо по SSH. Основание — [00-INFRA-0018](../adr/00-INFRA-20261113-0018-independent-roles-and-translation-mtls.md). Файлы controller binding/request принадлежат оператору и имеют mode `0600`. Unix-узлы требуют Python 3.13, собственный Docker Linux Engine для соответствующих владельческих процедур и заранее материализованный проверенный выпуск. Установка ОС/Python, перенос release archive, выдача PKI approvals и полная orchestration всех ролей остаются входами следующего уровня.

## Область интерфейса

Поддержаны самостоятельные SPIRE `install.sh` roles server/agent/observer/status-publisher/telemetry-sender, административные inspect/authorize/export-bootstrap, отдельный component `export.sh`, проверка initial bundle через `bootstrap.py validate` и Translator `station.py doctor|prepare|start|status --role provider|worker`. SPIRE images задаются immutable image IDs/digests. Совместный SPIRE observation/bootstrap-up и Translator bundle, `--update`, stop/revoke и произвольные shell/process commands этим интерфейсом не выполняются. `RESUME` отсутствует. Процедуры работают только с daemon и полномочиями своего целевого узла.

Это исполнитель одного явно подготовленного owner step, а не готовый полный distributed installer. Он не разрешает inventory, dependencies, routes, quality/image admission, replica fencing, актуальность public owner receipts или безопасную последовательность всех owner operations. Их обязан проверить вызывающий workflow до execute. Его `source_files` формируется из полного аутентифицированного release manifest после проверки архива; ручной неполный список не доказывает provenance или release admission. Экземпляры не делят межузловой journal/SVID volume. `state_root` выделяется одной логической owner installation; разные steps этой установки используют один state root и общий lock.

## Binding контроллера

Локальный binding содержит ровно `{"kind":"local"}`. SSH binding содержит ровно `kind="ssh"`, `host`, `user`, целый `port`, абсолютные локальные пути `identity_file`, `known_hosts_file`, `evidence_root`. Identity принадлежит текущему оператору с mode `0600`; known_hosts не доступен на запись группе/остальным. Host keys доставляются проверенным операторским каналом. TOFU, ssh-keyscan, password fallback, agent forwarding, пользовательский SSH config, ControlMaster и port forwarding не используются. Команда SSH запускает фиксированный Python carrier; shell quoting защищает сам carrier, а request передаётся JSON по stdin. Owner arguments не интерполируются в shell.

`evidence_root` принадлежит контроллеру, mode `0700`, вне source checkout. Каждая транспортная попытка сохраняет собственные stdout/stderr/result с mode `0600`, включая host-key failure/timeout; секретные diagnostics не включаются в публичный ответ. Сбой SSH оставляет `NODE_TRANSPORT_UNKNOWN_RECONCILE_BEFORE_RETRY`. Он не означает, что целевой процесс не успел начаться. Перед новым воздействием нужен `status` той же операции через восстановленный проверенный канал и владельческая сверка неопределённого результата.

## Request целевого узла

| Поле | Контракт |
|---|---|
| `schema_version` | Целое `1` |
| `operation` | Канонический UUID неизменяемого запроса |
| `source_root` | Абсолютный путь точного выпуска на целевом узле |
| `source_files` | Относительный file path → SHA-256; включены entrypoint, его зависимости и `tools/node_execution.py` |
| `state_root` | Абсолютный собственный технический каталог `0700` вне source root |
| `procedure` | `spire-install`, `spire-export`, `spire-bootstrap`, `translator-station` |
| `arguments` | Массив буквальных аргументов публичного entrypoint |
| `inputs` | Абсолютный target file path → SHA-256 всех закрытых подготовленных входов; файлы `0600` принадлежат target operator |
| `environment` | Только явно выбранные node-local Docker config/socket и SPIRE initial-input/authority/socket approval |
| `timeout_seconds` | Целое 1–7200 |

Синтаксис путей проверяет контроллер; существование, отсутствие symlinks, права и SHA проверяет целевой узел. Контроллер не разрешает Linux paths по собственной macOS filesystem. `source_files["tools/node_execution.py"]` совпадает с точными переданными байтами carrier module. Для Translator единственный `--env-file` обязательно закреплён в `inputs`, `--role` задаётся ровно один раз; повторные/альтернативные flags не могут выбрать bundle. Caller не передаёт plaintext secret values через arguments/environment. Inherited API keys, PYTHONPATH и DOCKER_CONTEXT не поступают owner process.

## Устойчивость и результат

До owner process атомарно и с `fsync` сохраняется `INTENT`, связанный с SHA-256 полного запроса. Общий `scope.lock` исключает одновременное исполнение разных operations одной installation. После выхода процесса сохраняются exit code и SHA-256 собственных stdout/stderr; сами bytes остаются в закрытом operation directory на целевом узле. `COMMAND_SUCCEEDED` означает только exit 0. `owner_readiness_verified=false` и `physical_distribution_verified=false` сохраняют границу доказательства.

Идентичный завершённый execute возвращает прежнюю квитанцию без повторного воздействия, но не доказывает текущую готовность владельца. Другое тело того же operation ID отклоняется. `INTENT`, `RESULT_UNKNOWN` и `OWNER_COMMAND_FAILED` не повторяют команду автоматически. Сохраняются прежние journals, recovery budget и Process Control. Изменение generation/config/image либо возобновление остановленной цели по-прежнему требует штатного owner handoff; новый operation ID не отменяет эти проверки владельца. `status` читает атомарную квитанцию, не создаёт отсутствующую target operation и не запускает owner process.

## Проверка

Набор `python3.13 -m unittest tools.test_node_execution` проверяет реальный child-process effect, повтор, scope lock, source/config drift, source symlink, interrupted intent, timeout, failure, exact-role guards, literal arguments и SSH policy. [Датированный отчёт](../audit/node-execution-20261114.json) отдельно фиксирует настоящий OpenSSH сеанс к изолированному Linux-контейнеру и вызов публичного SPIRE layout validator. Один физический компьютер, layout-only procedure и отсутствие полного inventory workflow указаны явно; это не физическая многоузловая приёмка.
