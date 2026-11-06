# Результат очистки остатков предыдущих тестов translator-service

- Дата: `2026-10-09`.
- Результат: `PASS`.
- Удалено: 10 остановленных контейнеров, 5 volumes и 3 сети.
- Сохранено: 22 чужих контейнера с прежними состояниями, все образы, рабочие `.env`, файловые секреты и кеш.

В [первой попытке реального запуска](lifecycle-image-failure.json) исходно отсутствовали контейнеры Kafka и переводчика. Перед удалением сверены точные ID оставшихся контейнеров, метки проектов, даты создания после начала этого теста, списки mounts и отсутствие чужих пользователей volumes. Три удаляемые сети не имели активных подключений. Все контейнеры уже были остановлены.

Адресно удалены `aelev-infrastructure-kafka-kafka-1` и девять ролей `aelev-infrastructure-translator`, volumes `aelev-infrastructure-kafka-data`, `aelev-infrastructure-translator-database`, `aelev-infrastructure-translator_translator-model-socket` и два анонимных Kafka volumes. Затем удалены сети `aelev-infrastructure-kafka`, `aelev-infrastructure-translator-data` и `aelev-infrastructure-translator-ingress`. Ни `prune`, ни принудительное удаление, ни перезапуск Docker daemon не выполнялись.

Финальная независимая сверка подтвердила сохранность всех 22 чужих контейнеров и всех 34 исходных образов вместе с их именами. Эти данные не следует сравнивать по порядку выдачи `docker image ls`: порядок строк не является состоянием ресурса.

Удалённые volumes содержали только данные созданного тестами стенда. Задания, переводы и Kafka-журнал этих прогонов удалены без возможности восстановления; сами тесты и сохранённые отчёты не удалены. Последующие прогоны нового теста учитывают свои ресурсы отдельно и очищают их автоматически.

```text
cleanup_containers=10/10
cleanup_named_volumes=3/3
cleanup_anonymous_volumes=2/2
cleanup_networks=3/3
foreign_containers_unchanged=22/22
existing_images_unchanged=34/34
working_environment_and_secrets=preserved
```
