# MinIO

S3-совместимое объектное хранилище инфраструктуры.

Одноразовая `bucket-provisioner` поддерживает opt-in [файловые credentials](USER_MANUAL.md#файловые-credentials-bucket-provisioner): два собственных read-only файла вместо inline password environment, non-TTY stdin для `mc` и закрытый отказ при partial/ambiguous inputs. Профиль не выбирается обычным запуском MinIO; bucket, policy и новые credentials принадлежат владельцу точной операции.

Для полного подготовленного workflow доступен image-only `compose.owner-image.yml`; точный образ связывается с владельческой build receipt по [SPEC](SPEC.md). Обычный профиль и его тег сохраняются.

Независимо размещённые экземпляры дополнительно выбирают `compose.routed.yml`: собственный native-порт передаётся через `server --address :PORT` и совпадает с published-портом, свой FQDN разрешается локально, остальные peers доступны через адреса назначенных узлов. Listener не задаётся различающимися `MINIO_ADDRESS`: закреплённый MinIO сверяет bootstrap environment peers. Публичные входы проверяет `prepare-routed.py`; подготовка и границы native-приёмки описаны в [руководстве](USER_MANUAL.md#независимое-размещение-с-явными-маршрутами).

## Основные возможности

- Две площадки по четыре узла.
- Репликация площадок и автоматически продлеваемые TLS leaf в каталогах владельца.
- Собственные данные и техническое состояние.

## Запуск

После подготовки по [руководству](USER_MANUAL.md), из этого каталога:

```sh
docker compose config --quiet
docker compose up -d --no-build --pull never
```

Первоначальная сборка, обновление и восстановление описаны в руководстве. При работе через общую инфраструктурную композицию не запускайте отдельный проект поверх тех же данных.

Builder использует canonical Go module versions закреплённых commits MinIO/`mc` через официальный module proxy с checksum verification; параметры и границы воспроизводимости описаны в [SPEC](SPEC.md).

## Навигация

- [architecture.mmd](architecture.mmd) — диаграмма границы, компонентов и основных потоков владельца.
- [USER_MANUAL.md](USER_MANUAL.md), [SPEC.md](SPEC.md) — использование и правила продукта.
- [INDEX.md](INDEX.md), [TASKS.md](TASKS.md), [CHANGELOG.md](CHANGELOG.md) — агентская карта, ограничения и история.
