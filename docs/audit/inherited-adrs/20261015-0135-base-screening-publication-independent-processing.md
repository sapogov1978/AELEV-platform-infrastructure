# ADR-0135: Публикация Base Screening не зависит от Web-обработки

> **Происхождение standalone-редакции.** Источник: `docs/adr/20261015-0135-base-screening-publication-independent-processing.md`; SHA-256: `31f822553de30f613a8b1eaf5201efe8720b6b56d536379ad9ce62696c14fec5`; основание включения: применимая зависимость ADR. Сохранены номер, статус, история замены и применимые решения.

- **Статус:** Принят по явному требованию оператора
- **Дата:** 2026-10-15
- **Основание:** A5, A10, A12, ADR-0001, ADR-0125, ADR-0128
- **Частично заменяет (Supersedes):** [ADR-0128](20261014-0128-base-screening-service-envelope.md), [ADR-0130](20261014-0130-base-screening-intake-without-web-processing.md) и [ADR-0132](20261015-0132-base-screening-inline-queue-processing-control.md) только в профиле `outbox-publisher`; разделение приёма, Web-обработки и восстановления сохраняется

## Контекст

Безопасный запуск Base Screening должен оставлять чтение публичных страниц выключенным до явного решения оператора. Прежняя поставка ошибочно распространяла это ограничение на `outbox-publisher`: уже сформированные и устойчиво сохранённые результаты оставались в `PENDING`, хотя их потребитель мог быть запущен и ожидать Kafka-сообщения. Состояние Web-обработки не является разрешением на доставку ранее принятых локальных решений.

## Решение

`outbox-publisher` входит в обычный Compose без профиля и поэтому также присутствует в режиме `intake`. Перед ним одноразовый `topic-provisioner` отдельной deployment identity создаёт и закрыто сверяет принадлежащий Base Screening topic `discovery.stage-commands.v1`; рабочая identity не получает административных полномочий. Publisher сохраняет собственную роль `outbox` в `RUNNING`, использует точную Kafka identity и публикует только уже существующие записи принадлежащего сервису outbox. Отсутствие Kafka ACK оставляет запись в `PENDING` с диагностикой и не изменяет payload или идентификатор. Topics запросов локализации остаются собственностью PMS и не дублируются в декларации Base Screening.

`triage-worker` остаётся постоянно доступным резервным процессом, но его роль `triage` после миграции имеет `PAUSED`. Кнопка «Включить/Выключить» управляет только выдачей заданий на ограниченное чтение и сравнение страниц. Она не запускает и не останавливает `outbox-publisher`, Kafka consumers, восстановительные процессы или инфраструктуру.

Профиль `intake` по-прежнему добавляет только `delta-consumer` и `policy-projection-consumer`; профиль `active` добавляет устойчивую входящую доставку и восстановление снимков. Ни запуск publisher, ни Kafka ACK не переводят `triage` в `RUNNING` и не создают Web-запрос.

## Последствия и проверка

Уже сформированное решение становится доступно следующей стадии независимо от того, включена ли последующая обработка новых страниц. Downstream-потребитель может быть запущен раньше или позже: Kafka сохраняет межсервисную асинхронность, а Base Screening завершает свою ответственность подтверждённой публикацией.

Проверка подтверждает успешный барьер `topic-provisioner`, наличие `outbox-publisher` в обычном и `intake`-составах Compose, `triage=PAUSED`, `outbox=RUNNING`, переход существующего `PENDING` в `PUBLISHED` после Kafka ACK и отсутствие новых Web-чтений.

## Миграция и откат

Новая миграция БД не требуется: роли `triage` и `outbox` уже имеют независимые строки `process_control`. Поставка обновляет Compose и запускает `docker compose --env-file .env up -d outbox-publisher`. Откат может остановить publisher без удаления outbox; после повторного запуска доставка продолжается с теми же идентификаторами.

## Связанные документы

- [Развёртывание](https://github.com/sapogov1978/discovery-research-platform/blob/8d1a663319ac15d8729cf1422858f37d57096c75/docs/architecture/21-deployment.md)
- [Каскад Discovery](https://github.com/sapogov1978/discovery-research-platform/blob/8d1a663319ac15d8729cf1422858f37d57096c75/docs/architecture/05-discovery-services.md)
- [Спецификация Base Screening](https://github.com/sapogov1978/discovery-research-platform/blob/8d1a663319ac15d8729cf1422858f37d57096c75/services/discovery-engine/base-screening/SPEC.md)
- [Руководство оператора](https://github.com/sapogov1978/discovery-research-platform/blob/8d1a663319ac15d8729cf1422858f37d57096c75/services/discovery-engine/base-screening/USER_MANUAL.md)
