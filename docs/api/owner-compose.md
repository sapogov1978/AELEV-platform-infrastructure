# Выполнение одной подготовленной Compose-роли, версия 1

Публичный [node executor](node-execution.md) поддерживает `procedure=owner-compose` по [00-INFRA-0019](../adr/00-INFRA-20261116-0019-pinned-owner-compose-execution.md). `arguments` содержит ровно `ACTION --request ABS_PRIVATE_JSON --request-sha256 SHA256`. ACTION — `plan`, `start`, `task` или `status`. Закрытый JSON закреплён в `request.inputs`; его `source_root/source_files` совпадают с outer request, а `inputs` равны остальным outer inputs. JSON проверяется по переданному SHA и разбирается из одних bytes; произвольные команды/flags не принимаются.

| Поле private definition | Значение |
|---|---|
| `schema_version` | Целое 1 |
| `source_root`, `source_files` | Полный source root/regular-file SHA manifest, предварительно аутентифицированный установщиком; включает оба helpers, каталог и recipes |
| `catalog`, `role` | Относительный source path каталога и точный `product/role-id` |
| `recipes` | Непустой упорядоченный subset recipes данной роли; пути закреплены source manifest |
| `project_directory`, `project` | Закрытый подготовленный node-local owner workspace вне source и явный Compose project |
| `env_file`, `inputs` | Absolute private env path и absolute regular input path → SHA; private files принадлежат оператору, mode 0600 |
| `image` | Registry `name@sha256:…` или native `sha256:…`; start/task не выполняют build/pull |
| `configuration_sha256` | Hash выбранного resolved service из `plan`; для plan допустим null |

Владелец предварительно доставляет private inputs и готовит workspace под относительные пути своего рецепта. Исполнитель не копирует чужие keys и не создаёт PKI/egress/quality admission. Source paths, configured endpoints, image/command/entrypoint не подменяются. Только процессы выбранного узла используют его Docker daemon. Parent executor исключает наследование API keys, PYTHONPATH и Docker context overrides.

`plan` проверяет source/input pins и recipe membership, выполняет `compose config --format json` и возвращает configuration hash без старта и image pull. Resolved model сохраняется в private временном JSON с mode 0600; следующие команды используют только этот snapshot и пустой explicit env. Изменение исходного env после render не меняет запускаемый model. Секретные поля model и raw process output не входят в публичный JSON.

`start` допускается только для runtime/node role. До воздействия проверяются configuration hash, native image ID, выбранные project/service labels и число прежних экземпляров. Running instance с точными bindings сохраняется. Отсутствующая роль запускается с `up --detach --no-deps --no-build --pull never --no-recreate SERVICE`; её точный ID/image/config проверяются после. Stopped/paused/restarting instance, другой image/config или лишняя replica закрыто требуют handoff. Native restart, service namespaces, links и volumes_from отвергаются. `status` читает ту же привязку без старта; health — только наблюдаемый Docker health field, не внешний readiness proof.

`task` допускается только для operation role без существующего контейнера той же project/service. Используется `run --rm --no-deps --pull never --no-tty --interactive=false SERVICE`; дополнительных command/entrypoint/environment arguments нет. Предварительный exact image inspect исключает implicit missing-image build/pull. Exit code задачи становится outcome узлового журнала; повтор/unknown обрабатываются прежним node executor, не автоматическим повтором задачи.

Ошибка Docker/Compose сохраняет точный `owner_exit_code` в закрытом child result. Через node executor bounded stdout/stderr команды остаются в `owner-command-failure.json` с mode 0600 внутри текущего operation directory; публичный JSON не содержит raw diagnostics. Внутренний путь задаёт сам node executor и не наследуется Docker-процессом. `pull_policy=build` и несовпадающая фактическая native restart policy закрыто отклоняются.

Результаты `OWNER_COMPOSE_PLANNED`, `OWNER_ROLE_RUNNING`, `OWNER_ROLE_STOPPED`, `OWNER_ROLE_ABSENT` и `OWNER_TASK_COMPLETED` сохраняют `owner_readiness_verified=false`, `queue_resume_issued=false`. Общий inventory DAG, native identity admission, live readiness, полный platform E2E и исполняемая подготовка каждого владельца этим интерфейсом не закрываются. Физическая проверка вынесена в [TODO-MVP](../../TODO-MVP.md); остальные software gates сохраняются.

До реализации объявлен прогноз 60–90 минут на criteria/код/проверки. Scope сверяется с placement и node artifact/execution API, прежними immutable source generations и owner recipes. Стек, source каталоги, endpoints и работающие установки сохраняются.
