# PostgreSQL: карта каталога

Эта агентская карта дополняет Graphify высокоуровневым ответом на вопрос «что и где искать» в каталоге владельца.

| Что искать | Где находится |
|---|---|
| Назначение, кластер и запуск | [README.md](README.md) |
| Подготовка, эксплуатация и восстановление | [USER_MANUAL.md](USER_MANUAL.md) |
| Нормативные гарантии | [SPEC.md](SPEC.md) |
| Выбор и проверка ограждения | [FENCING.md](FENCING.md) |
| Кандидат независимого SSH fencer и одноразовой узловой операции | [FENCING_SSH.md](FENCING_SSH.md), `compose.fencing-ssh.yml`, `fencing-node.py` |
| Настоящий PostgreSQL/Patroni/etcd через published endpoints и SSH, crash/replay двух controllers | `test/fencing-ssh-lifecycle.py`, `.github/workflows/postgresql-fencing-ssh.yml` |
| Детерминированный доэффектный crash point первого controller key | `test/ha_return_barrier.py`, `test/test_ha_return_barrier.py`; только SSH host fixture, без runtime override |
| Ручной подписанный multiarch-выпуск после exact-source audit/HA | `.github/workflows/postgresql-fencing-release.yml`; допуск опубликованного digest проверяется отдельно |
| Текущие задачи и допуск площадки | [TASKS.md](TASKS.md) |
| История изменений | [CHANGELOG.md](CHANGELOG.md) |
| Настройки и запуск | `.env.example`, `compose.yml` |
| Проверки и результаты | `test/` |
| Автономный возврат точных узлов после fencing, отсутствие аренды и сохранение бюджета | [Протокол 20 октября](test/fencing-autonomous-return-result-20261020.md) |

Кластер и ограждение определены [00-INFRA-20261106-0006-technical-storage-boundaries](../docs/adr/00-INFRA-20261106-0006-technical-storage-boundaries.md).
