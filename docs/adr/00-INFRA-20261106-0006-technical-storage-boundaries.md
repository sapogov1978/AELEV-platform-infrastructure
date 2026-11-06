# 00-INFRA-20261106-0006-technical-storage-boundaries — Границы технических хранилищ

- **Статус:** Принят
- **Дата:** 2026-11-06
- **Происхождение:** неизменяемые ADR-0042, ADR-0092, ADR-0093, ADR-0098, ADR-0106 и ADR-0115 из [архива выделения](../audit/inherited-adrs/INDEX.md)

## Контекст

Разные типы состояния требуют разных механизмов, но выбор технологии не должен создавать общую изменяемую базу между владельцами.

## Ключевые решения

- PostgreSQL предоставляет серверное транзакционное хранилище; конкретные databases, schemas, roles и migrations принадлежат потребителям.
- MinIO предоставляет техническое объектное хранилище; buckets, lifecycle rules и credentials конкретного приложения принадлежат потребителям.
- SQLite разрешён только для локального технического журнала, checkpoint, cache, sequence и single-node metadata одного владельца.
- Устойчивый SQLite использует WAL, `synchronous=FULL` и проверяемый backup/restore; общий файл, NFS/SMB и чтение соседнего владельца запрещены.
- PostgreSQL fencing выбирается явным профилем и не отключается корневой композицией.
- Репликация, WAL и erasure coding не считаются резервной копией; восстановление проверяется отдельно.

## Последствия

- Технология соответствует характеру нагрузки без размытия владельческих границ.
- Прикладные данные не появляются в корневом Compose или общем SQLite.
- Географический и HA-профиль требует отдельного допуска площадки.

## Проверка и откат

Storage-boundary tests запрещают общие database files и application resources. PostgreSQL и MinIO проходят owner-local recovery checks; SQLite проверяет режимы PRAGMA, целостность журнала и восстановление копии. Откат не удаляет volumes и не понижает формат данных без владельческой миграции.

## Официальные основания

- [PostgreSQL standby](https://www.postgresql.org/docs/17/warm-standby.html) и [continuous archiving](https://www.postgresql.org/docs/17/continuous-archiving.html) описывают репликацию и восстановление WAL.
- SQLite документирует [WAL](https://www.sqlite.org/wal.html), [backup API](https://www.sqlite.org/backup.html) и [PRAGMA synchronous](https://www.sqlite.org/pragma.html#pragma_synchronous).
- MinIO документирует [erasure coding](https://min.io/docs/minio/linux/operations/concepts/erasure-coding.html) и [deployment topologies](https://min.io/docs/minio/linux/operations/installation.html).
