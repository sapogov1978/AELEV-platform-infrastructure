# 00-INFRA-20261117-0020-pinned-owner-source-build — Отдельная сборка владельческого образа

- **Статус:** Принят как реализация подтверждённого плана автоматической подготовки поставки по 00-INFRA-0018
- **Дата:** 2026-11-17
- **Уточняет:** 00-INFRA-0004, 00-INFRA-0018, 00-INFRA-0019; допуск нового выпуска остаётся отдельным gate

## Контекст

Подготовленный owner-compose требует существующий immutable image. Полная установка с чистого checkout должна отдельно собрать принадлежащую владельцу роль на назначенном узле и закрепить результат, сохранив разделение build/start и непрозрачность внешних providers. Сборка из рабочего дерева целиком может передать Docker private inputs, даже если они исключены из Git.

## Ключевые решения

- Публичная operator-процедура `owner-build` использует прежний local/SSH node executor, точные независимые executor/owner manifests, catalog role, recipe/target/context, native platform и явные несекретные build arguments. Каталогов приложений внутри инфраструктурного кода нет.
- Opaque roles не собираются этим API: они по-прежнему требуют опубликованный exact provider image. Context находится внутри owner directory; recipe входит в source manifest и закреплён отдельным SHA. Временный build context содержит только проверенные manifest files с повторной проверкой копируемых bytes и явным hash paths/bytes/modes. Неперечисленные env, credentials и runtime data не передаются builder.
- Build выполняется без tag, push, runtime start, secret/SSH forwarding либо дополнительных host entitlements. Native platform проверяется до команды и у результата; скрытая эмуляция не допускается. Принятые Dockerfiles, OS/Python/dependency lines и настройки workload не меняются.
- Квитанция связывает owner manifest, recipe/target/context/build arguments/platform, context hash, BuildKit output ID и фактический native image ID. Raw build logs и максимальная metadata/provenance остаются в закрытом node operation directory. Типизированная public projection содержит только IDs/hashes/platform/status.
- Прежний fsync intent/receipt сериализует build operation. Повтор завершённого request возвращает receipt; failure и unknown не выполняют build повторно. Timeout остаётся RESULT_UNKNOWN с обязательным reconciliation. Receipt не доказывает, что image всё ещё существует после удаления оператором; потребитель обязан проверить наличие до start.
- Build receipt не является security/release/quality admission, не подтверждает dependency readiness и не выполняет RESUME. Предметные очереди, canonical storage и recovery budgets не принадлежат builder.

## Последствия

Установщик может подготовить и связать source-built artifact с inventory без второго build backend, импорта provider source или изменения работающих образов. Context snapshot требует временное место; отсутствие dependencies/registry access, неподдерживаемый target и неправильная native platform закрывают этот этап. Это generic техническая процедура, не новый владелец функциональных сервисов.

## Проверка и откат

Проверяются настоящая сборка из snapshot, image inspect, private output, input/source drift, opaque refusal, отсутствие посторонних файлов, несовпадающая архитектура, known failure/unknown и повтор node operation без второго эффекта. Controller связывает receipt со своим source/inventory до последующего owner-compose. Старые procedures и receipts сохраняются; откат helper не удаляет images, source generations или unfinished journals.

## Официальные основания

- [Build context](https://docs.docker.com/build/concepts/context/) определяет доступные builder файлы; конкретную manifest-only политику задаёт продукт.
- [BuildKit build](https://docs.docker.com/reference/cli/docker/buildx/build/) определяет target/platform/iidfile/metadata/load; receipt не объявляется независимой SLSA аттестацией.
- [Docker build secrets](https://docs.docker.com/build/building/secrets/) объясняет, почему credentials не передаются обычными build arguments. Этот API не принимает secret forwarding.
- [SLSA provenance](https://slsa.dev/spec/v1.2/provenance) связывает build outputs с источниками; уровень SLSA этим локальным механизмом не заявляется.
- [Python TimeoutExpired](https://docs.python.org/3.13/library/subprocess.html#subprocess.TimeoutExpired) не устанавливает отсутствие внешнего эффекта после timeout.
