# PostgreSQL: карта каталога

| Что искать | Где находится |
|---|---|
| Назначение, кластер и запуск | [README.md](README.md) |
| Подготовка, эксплуатация и восстановление | [USER_MANUAL.md](USER_MANUAL.md) |
| Нормативные гарантии | [SPEC.md](SPEC.md) |
| Выбор и проверка ограждения | [FENCING.md](FENCING.md) |
| Текущие задачи и допуск площадки | [TASKS.md](TASKS.md) |
| История изменений | [CHANGELOG.md](CHANGELOG.md) |
| Настройки и запуск | `.env.example`, `compose.yml` |
| Проверки и результаты | `test/` |
| Автономный возврат точных узлов после fencing, отсутствие аренды и сохранение бюджета | [Протокол 20 октября](test/fencing-autonomous-return-result-20261020.md) |

Кластер и ограждение определены [ADR-0098](../docs/adr/20261009-0098-clustered-infrastructure-postgresql.md) и [ADR-0106](../docs/adr/20261010-0106-selectable-postgresql-fencing.md).
