# Kafka: карта каталога

Эта агентская карта дополняет Graphify высокоуровневым ответом на вопрос «что и где искать» в каталоге владельца.

| Что искать | Где находится |
|---|---|
| Назначение, топология и запуск | [README.md](README.md) |
| Подготовка, эксплуатация и восстановление | [USER_MANUAL.md](USER_MANUAL.md) |
| Нормативные гарантии | [SPEC.md](SPEC.md) |
| Текущие задачи и допуск площадки | [TASKS.md](TASKS.md) |
| История изменений | [CHANGELOG.md](CHANGELOG.md) |
| Настройки и запуск | `.env.example`, `compose.yml` |
| Проверки и результаты | `test/` |
| Минимальная нативная проба, точный прежний runtime и сборка | `health.c`, `Dockerfile`, [спецификация](SPEC.md#нативная-проба-brokercontroller) |
| Изолированный жизненный тест, рабочая выкладка и проверка реальной копии | [Результат 21 октября](test/native-health-result-20261021.md), `test/native_health_docker.py`, `test/cold_backup_verify.py` |

Владение topics и межсервисный транспорт определяет [00-INFRA-20261106-0005-kafka-cloudevents-contracts](../docs/adr/00-INFRA-20261106-0005-kafka-cloudevents-contracts.md).
