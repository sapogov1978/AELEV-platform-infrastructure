# Сверка инфраструктурных строк в настоящем браузере

Дата: 2026-10-10T09:47:23.549598+00:00. Итог: PASS.

Команда: `python3.13 test/status_overview.py --expected-module-count 13` с обязательными URL, Chromium, учётной записью просмотра и файлом её пароля по README.

| Назначение, объявленное владельцем | Ревизия | Состояние | Состав владельца |
|---|---|---|---|
| Интерфейс платформы | 2 | ONLINE | Оболочка / nginx, API состояния, Издатель собственного состояния |
| Панели наблюдения и уведомления | 2 | ONLINE | Grafana, Издатель состояния Grafana |
| Хранилище структурированных данных | 2 | ONLINE | PostgreSQL / Patroni-1, etcd-1, PostgreSQL / Patroni-2, etcd-2, PostgreSQL / Patroni-3, etcd-3, Издатель состояния PostgreSQL |
| Доставка телеметрии | 1 | ONLINE | Доставка телеметрии, Издатель состояния Telemetry delivery |
| Обработка телеметрии | 2 | ONLINE | OpenTelemetry Collector, Издатель состояния OpenTelemetry Collector |
| Хранение и поиск трасс | 2 | ONLINE | Tempo, Издатель состояния Tempo |
| Хранилище метрик | 2 | ONLINE | Prometheus, Издатель состояния Prometheus |
| Перевод текстов | 2 | ONLINE | PostgreSQL переводчика, Соединение с Kafka, Приоритетный consumer, Обычный consumer, MADLAD, Исполнитель перевода, Outbox, Операционный API, Издатель состояния переводчика |
| Межсервисное взаимодействие | 2 | ONLINE | Kafka broker / controller, Издатель состояния Kafka |
| Проверка доступности | 2 | ONLINE | Blackbox Exporter, Издатель состояния Blackbox Exporter |
| Хранилище артефактов | 2 | ONLINE | MinIO SITE-A-1, MinIO SITE-A-2, MinIO SITE-A-3, MinIO SITE-A-4, MinIO SITE-B-1, MinIO SITE-B-2, MinIO SITE-B-3, MinIO SITE-B-4, Издатель состояния MinIO |
| Хранение и поиск журналов | 2 | ONLINE | Loki, Издатель состояния Loki |
| Идентификация и доступ | 3 | ONLINE | Keycloak, Keycloak Database, Keycloak Status Publisher |

Все строки получены динамически через настоящий OIDC/API. Названия назначения и компонентов обоих языков сверены с DOM; раскрытие, сворачивание и сохранение раскрытия при смене языка проверены. Все компоненты показаны ONLINE. Тест не содержит списка продуктов или их адресов, не создаёт и не изменяет ресурсов. [Снимок страницы](status-overview.png). Отдельные stop/start и независимость публикаций подтверждаются только тестами владельцев, не этим снимком. WAN и продукционная PKI не проверялись.
