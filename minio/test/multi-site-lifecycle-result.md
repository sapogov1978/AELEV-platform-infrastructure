# Результат проверки жизненного цикла MinIO

- Время: `2026-10-08 23:06:26–23:13:25 CEST`.
- Команда: `services/infrastructure/minio/test/multi-site-lifecycle.test.sh`.
- Результат: `PASS`, код завершения `0`.
- Проект Docker Compose: `aelev-minio-test-20261008210626-17555`.
- MinIO: `RELEASE.2025-04-22T22-12-26Z`, commit `0d7408fc9969caf07de6a8c3a84f9fbb10a6739e`.
- `mc`: `RELEASE.2025-04-16T18-13-26Z`, commit `b00526b153a31b36767991a4f5ce2cced435ee8e`.
- Собранный рабочий образ: `aelev/minio-server:RELEASE.2025-04-22T22-12-26Z`, image ID `sha256:7fb209a14a06f8e6b91de3ef8f3fd7dc14e178d4212cc613ce838632e7ec6e38`.
- Размер исходного объекта: `2048 MiB`.
- SHA-256 исходника и объектов на обеих площадках: `a7c744c13cc101ed66c29f672f92455547889cc586ce6d44fe76ae824958ea51`.

Тест собрал закреплённые MinIO и `mc`, сверил встроенные release и commit, поднял две независимые четырёхузловые TLS-площадки с `EC:2` и два тестовых Nginx-балансировщика, настроил site replication и создал принадлежащие тесту bucket, пользователя и policy.

Объект размером 2 ГиБ загружался через стабильный endpoint площадки A с ограничением скорости. Пока загрузка оставалась активной, тест по очереди остановил и восстановил каждый из восьми узлов. Клиент повторял прерванные S3-запросы через тот же endpoint; после окончания загрузки объект стал доступен на обеих площадках, а SHA-256 исходника и двух удалённых копий совпал.

После одновременной остановки двух узлов площадки A её штатный cluster healthcheck перешёл в `unhealthy`, подтвердив потерю write quorum. После восстановления узлов тест полностью остановил и повторно запустил восемь MinIO-контейнеров и оба балансировщика, повторно сверил хэши на обеих площадках и подтвердил идемпотентность одноразовой настройки site replication.

В конце тест явно остановил всю топологию и удалил созданные контейнеры, две сети, восемь volumes, временные TLS-материалы, исходный файл и загруженный только для теста образ Nginx. Независимая проверка не обнаружила контейнеров, сетей, volumes или временных каталогов с префиксом `aelev-minio-test-`. Собранный `aelev/minio-server` сохранён как рабочий результат сборки, а два временных диагностических builder-образа удалены отдельно.

Проверенные состояния:

```text
vendor_versions=verified
initial_nodes_healthy=8/8
site_replication=verified
bucket_access_site_a=verified
bucket_access_site_b=verified
sequential_node_cycles_during_upload=8/8
source_sha256=a7c744c13cc101ed66c29f672f92455547889cc586ce6d44fe76ae824958ea51
site_a_sha256=verified
site_b_sha256=verified
write_quorum_loss=unhealthy
complete_topology_restart=verified
object_after_restart_site_a=verified
object_after_restart_site_b=verified
site_replication_reapply=idempotent
cleanup_containers=removed
cleanup_networks=removed
cleanup_volumes=removed
cleanup_temporary_files=removed
cleanup_test_pulled_image=removed
```
