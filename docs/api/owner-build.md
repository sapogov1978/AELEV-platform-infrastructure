# Отдельная сборка владельческого source image, версия 1

`procedure=owner-build` узлового [executor](node-execution.md) по [00-INFRA-0020](../adr/00-INFRA-20261117-0020-pinned-owner-source-build.md) получает `arguments=["build","--request",ABS_PRIVATE_JSON,"--request-sha256",SHA256]`. Definition входит в outer `inputs`; его executor root/manifest совпадают с outer source binding, `inputs` внутри definition — пустой объект. Все public source files предварительно доставляются отдельной immutable generation; JSON имеет mode `0600` и принадлежит operator UID. Node daemon выбирается прежним local/SSH binding, а не соседним checkout.

| Поле private definition | Контракт |
|---|---|
| `schema_version` | Целое `1` |
| `executor_root`, `executor_files` | Exact infrastructure root и полный regular-file SHA manifest с node_execution, owner_compose и owner_build helpers |
| `owner_root`, `owner_files` | Отдельный exact owner source и полный regular-file SHA manifest |
| `catalog`, `role` | Manifest path каталога и точный `product/role-id`; `opaque` обязан быть false |
| `context` | Нормализованный source-relative directory внутри объявленного owner; `.` допустим для owner корня |
| `recipe`, `source_sha256` | Manifest path Dockerfile внутри context и `sha256:` raw file digest; совпадает с inventory build descriptor |
| `target`, `platform` | Явный Docker stage и `linux/amd64` либо `linux/arm64` |
| `build_arguments` | Не более 64 явно объявленных Dockerfile ARG, строки до 4096 bytes; credentials в них запрещены |
| `inputs` | Ровно `{}`; env, keys и runtime не являются build inputs |

Context копирует только manifest files, повторно проверяет каждый SHA и сохраняет regular executable/read-only source modes как `0755`/`0644`. Публичные каталоги snapshot имеют `0755`, в том числе при operator umask `0077`: после Docker COPY их должен читать непривилегированный runtime UID. Его закрытый operation ancestor остаётся `0700`; iid/metadata/logs — `0600`. Context hash включает file paths/bytes/modes и фиксированный directory mode. Неперечисленные файлы checkout не попадают в snapshot. Ограничения: 20 000 files, 32 MiB/file, 256 MiB суммарно; symlinks/traversal закрыто отклоняются. Snapshot находится в закрытом operation directory вне обоих source roots и удаляется после команды. Полный source manifest проверяется прежде, чем выбирается context. Source и runtime state не смешиваются.

Build arguments выбирает владелец по своему каноническому recipe; builder не переписывает base images или endpoints. API отвергает неизвестный ARG, имена с PASSWORD/SECRET/TOKEN/API_KEY/PRIVATE_KEY, NUL/newline и URL credentials. Этот дополнительный фильтр не классифицирует произвольный текст как безопасный: вызывающий владелец не передаёт секретные значения вообще. Build secrets/SSH mounts и дополнительные contexts/entitlements этим API не представлены. [Docker secrets](https://docs.docker.com/build/building/secrets/) не рекомендует ARG для credentials.

До build читается только native daemon OS/architecture; другая платформа закрыто получает `OWNER_BUILD_NATIVE_PLATFORM_REQUIRED`. [BuildKit](https://docs.docker.com/reference/cli/docker/buildx/build/) выполняет `buildx build --load --progress plain --file SNAPSHOT_RECIPE --target TARGET --platform PLATFORM --iidfile PRIVATE_ID --metadata-file PRIVATE_METADATA ... SNAPSHOT`. Tag/push/up/run/restart не вызываются. Сборка Dockerfile выполняет свои RUN steps в builder, но не запускает workload из inventory. Ранее работающие теги не меняются. Возможные скачивания base images/dependencies — обычные объявленные inputs владельческого Dockerfile, а не получение provider source или ключа.

После успеха `image inspect` точного output ID возвращает только native ID/OS/architecture. `OWNER_IMAGE_BUILT` содержит `native_image_id`, `build_output_id`, `context_sha256`, `parameters_sha256`, `platform`, `build_metadata_sha256`. Parameters hash включает полный owner manifest и recipe/context/source SHA/target/platform/явные build arguments. BuildKit metadata/provenance, iidfile и process logs остаются в operation directory с mode `0600`; публичная typed projection не копирует raw text. Это локальная provenance binding, не подписанная SLSA аттестация или гарантия битовой воспроизводимости внешних mutable package repositories.

Успех сохраняет `image_admitted=false`, `owner_readiness_verified=false`, `queue_resume_issued=false`. Node receipt не отменяет необходимость текущего inspect image перед start, отдельного security/stack/quality admission и readiness зависимостей. Opaque provider image остаётся exact published artifact. Тесты одного Linux daemon не считаются proof разных физических компьютеров.

Build может работать до 6600 секунд; outer node timeout выбирается явно до прежнего предела 7200 секунд и должен включать overhead. Известный exit build сохраняется как owner failure; timeout child возвращает reserved exit 3 и RESULT_UNKNOWN. Родительский timeout также остаётся unknown. Повтор той же operation не пересобирает image; reconcile не использует новый operation ID для обхода неизвестного результата. Прогноз первого блока code/регрессии/настоящая source build до правок — 60–90 минут. [Context documentation](https://docs.docker.com/build/concepts/context/) и [SLSA provenance](https://slsa.dev/spec/v1.2/provenance) задают механизмы и практику, а границы admission — этот продукт.
