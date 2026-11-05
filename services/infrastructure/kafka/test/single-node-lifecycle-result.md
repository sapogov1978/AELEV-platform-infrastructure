# Результат жизненного теста Kafka

- Дата и время: 2026-10-08 15:13 CEST
- Команда: `services/infrastructure/kafka/test/single-node-lifecycle.test.sh`
- Образ: `apache/kafka:3.9.1`
- Тестовый Compose project: `aelev-kafka-test-20261008130942-88741`
- Результат: `PASS`

Тест поднял одноузловой Kafka broker, дождался состояния `healthy`, явно создал принадлежащий тесту topic, записал и прочитал первое сообщение, остановил контейнер, повторно поднял тот же контейнер, подтвердил сохранность первого сообщения, записал и прочитал второе сообщение и снова остановил контейнер.

После проверки тест выполнил `docker compose down --volumes --remove-orphans`. Встроенные проверки подтвердили удаление тестового контейнера, сети и data volume. Образ до теста отсутствовал, поэтому загруженный только для этого прогона `apache/kafka:3.9.1` также удалён.

Независимая проверка после завершения не обнаружила контейнеров, сетей и volumes с префиксом `aelev-kafka-test-`; образ `apache/kafka:3.9.1` отсутствует.

Проверенные состояния:

```text
availability_before_restart=healthy
message_before_restart=verified
container_stopped=verified
availability_after_restart=healthy
persisted_message_after_restart=verified
new_message_after_restart=verified
cleanup_container=removed
cleanup_volume=removed
cleanup_network=removed
cleanup_image=removed_test_pull
```
