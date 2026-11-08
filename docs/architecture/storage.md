# Границы хранения инфраструктуры

Нормативный выбор технологий закреплён [00-INFRA-20261106-0006-technical-storage-boundaries](../adr/00-INFRA-20261106-0006-technical-storage-boundaries.md). Инфраструктура не определяет предметную схему потребителя и не получает право читать чужое каноническое состояние.

| Состояние | Поставка и правила | Эксплуатация и проверка |
|---|---|---|
| Серверные транзакционные данные | [PostgreSQL SPEC](../../postgresql/SPEC.md); базы, роли и migrations конкретного приложения принадлежат ему | [Руководство](../../postgresql/USER_MANUAL.md), [ограждение](../../postgresql/FENCING.md); strict quorum, failover и физическое восстановление проверяет owner lifecycle |
| Крупные неизменяемые объекты | [MinIO SPEC](../../minio/SPEC.md); потребитель владеет buckets, credentials и retention | [Руководство](../../minio/USER_MANUAL.md); site replication не заменяет независимый backup |
| Технические журналы, checkpoints и проекции одного владельца | Owner-local SQLite, без общего файла между сервисами | WAL, `synchronous=FULL`, проверяемая резервная копия и восстановление |
| Очередь и результат технического перевода | Собственный PostgreSQL Translator | [Руководство](../../translator-service/USER_MANUAL.md); отсутствие провайдера сохраняет задания, dedup и исходные байты |
| Неподтверждённая телеметрия и проекции | [Telemetry Delivery](../../observability/delivery/SPEC.md) и продукты наблюдения | [Протокол доставки](../api/telemetry.md); квитанции, replay и восстановление принадлежат владельцу |

## Встроенное техническое хранилище SQLite

SQLite разрешён для локального технического состояния одного владельца: sequence, UUID, checkpoint, cache, outbox и single-node metadata. Общий файл, чтение чужого SQLite и размещение на NFS/SMB запрещены. Устойчивый журнал использует WAL и `synchronous=FULL`; локальный cache не объявляется канонической предметной базой.

Ротация сертификата, обновление образа, отказ healthcheck или повторный `init` не удаляют volumes, истории, identities и неподтверждённые байты. Совместимость схемы и rollback проверяются владельцем на сохранённой копии до допуска писателей. Репликация, WAL и erasure coding не заменяют независимую резервную копию. Продуктовый [clean-room gate](../operations/clean-room-reproduction.md) проверяет поддерживаемый локальный путь; восстановление после утраты площадки, PITR, независимый backup target и RPO/RTO проверяются отдельно на целевой установке.

## Официальные основания

- [PostgreSQL continuous archiving](https://www.postgresql.org/docs/17/continuous-archiving.html).
- SQLite: [WAL](https://www.sqlite.org/wal.html), [PRAGMA synchronous](https://www.sqlite.org/pragma.html#pragma_synchronous), [backup API](https://www.sqlite.org/backup.html).
