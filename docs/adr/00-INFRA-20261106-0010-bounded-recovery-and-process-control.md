# 00-INFRA-20261106-0010-bounded-recovery-and-process-control — Ограниченное восстановление и управление процессами

- **Статус:** Принят
- **Дата:** 2026-11-06
- **Происхождение:** неизменяемые ADR-0072, ADR-0148, ADR-0155 и ADR-0171 из [архива выделения](../audit/inherited-adrs/INDEX.md)

## Контекст

Автоматический restart без общей истории может повторять разрушительное воздействие, игнорировать ручную остановку и скрывать неизвестный исход предыдущей попытки.

## Ключевые решения

- Восстановление выполняет единственный хостовый контроллер только для явно зарегистрированного owner-local процесса.
- История попыток, expected fingerprint, incarnation, manual stop, circuit state и replacement handoff сохраняются устойчиво.
- Воздействие начинается только после `PAUSE`, нулевых активных leases и повторной проверки точной цели.
- Неизвестный исход, смена identity, исчерпанный budget или другой писатель закрыто блокируют дальнейшее действие.
- Native restart policy конечна или равна `no`; новый container ID не обнуляет накопленные попытки.
- `RESUME`, restart, replacement release и снятие circuit являются разными явно подтверждаемыми полномочиями.
- Восстановление одного owner не запускает, не останавливает и не изменяет очереди или ресурсы другого.

## Последствия

- Автоматизация остаётся ограниченной и аудируемой вместо бесконечного restart-loop.
- Ручная остановка и защитные удержания переживают reboot и пересоздание контейнера.
- Operator сохраняет контроль над возобновлением предметной обработки.

## Проверка и откат

Recovery tests покрывают stale identity, target replacement, exhausted budget, concurrent writer, manual stop, unknown outcome и сохранение истории. Откат контроллера не удаляет journal и не переводит PAUSE в RESUME.

## Официальные основания

- [Docker restart policies](https://docs.docker.com/engine/containers/start-containers-automatically/) описывают семантику `no`, `on-failure`, `always` и `unless-stopped` и их ограничения.
- [Compose restart](https://docs.docker.com/reference/compose-file/services/#restart) задаёт declarative container policy, но не заменяет внешний аудит попыток.
- [SQLite WAL](https://www.sqlite.org/wal.html) и [backup API](https://www.sqlite.org/backup.html) описывают устойчивое owner-local хранение журнала контроллера.
