# Результат проверки повторного provisioning Kafka topics переводчика

- Время: `2026-10-09 09:20:43 CEST` (`Europe/Belgrade`).
- Команда проверки: `poetry run pytest -q test/test_runtime.py::test_existing_topics_are_verified_without_empty_creation`.
- Результат: `PASS`, одна проверка пройдена, ошибок и пропусков нет.
- Длительность: `0,526 с`.
- Среда: сервисный образ `aelev/translator-service:test`, Python `3.13.7`.

Проверка воспроизводит ветвь повторного запуска `translator-topic-provisioner`, когда все объявленные в проверяемом наборе topics уже существуют. Модульный сценарий использует один существующий topic `translation.requests.v1`. В первом полном прогоне эта ветвь приводила к ошибке `ValueError: Expected non-empty list of NewTopic objects`: provisioner вызывал `AdminClient.create_topics([])` и не позволял сервису восстановиться после перезапуска.

После исправления provisioner не вызывает создание для пустого списка, но продолжает сверять число partitions, replication factor и параметры каждого существующего topic. Тестовый Kafka AdminClient отвергает любую попытку создания в этом сценарии и подтверждает выполнение проверки конфигурации объявленного topic. Исправление не отключает проверку инициализации ради успешного результата.

Это модульная регрессия с тестовым AdminClient, без запуска нового broker, PostgreSQL или модели. Контейнер одноразового запуска pytest удалён; тест не создавал инфраструктурных topics, сетей или volumes. Проверка повторного provisioning на настоящей Kafka выполнена отдельно в полном жизненном цикле и описана в [общем отчёте](validation-result.md).

Проверенные состояния:

```text
existing_topics=1/1
empty_create_topics_call=not_called
existing_topic_topology=verified
existing_topic_configuration=verified
tests_passed=1
tests_failed=0
errors=0
tests_skipped=0
duration_seconds=0.526
```

Исходный машинный результат сохранён в архиве JUnit. Дата, длительность и итог проверки сохранены; повторный запуск при переоформлении не выполнялся.
