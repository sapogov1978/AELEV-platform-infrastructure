# ADR-0159: Видимый read-only срез Deep Screening до предметного runtime

> **Происхождение standalone-редакции.** Источник: `docs/adr/obsolete/20261027-0159-deep-screening-visible-read-only-slice.md`; SHA-256: `e474df78c76ec543bbc3c9b60146390c69605a35a629953dca698bebb95bfd75`; основание включения: прямая ссылка из поставки. Сохранены номер, статус, история замены и применимые решения; упоминания исключённых bounded contexts нейтрализованы без переноса их контрактов.

- **Статус:** Заменён [ADR-0160](../20261027-0160-deep-screening-owner-injected-presentation.md) в владельческой инъекции интерфейса, состояния и метрик, [ADR-0162](../20261027-0162-deep-screening-operator-presentation.md) и [ADR-0164](../20261027-0164-deep-screening-literal-advance-layout.md) в операторском представлении и [ADR-0168](../20261028-0168-deep-screening-durable-sensor-surface-pipeline.md) в устойчивом приёме, обработке, Process Control и публикации; первый read-only срез сохраняется только как история
- **Дата:** 2026-10-27
- **Основание:** A5, A10, A12, ADR-0082, ADR-0110, ADR-0157, ADR-0158
- **Частично заменяет (Supersedes):** ADR-0158 только в запрете запуска всей поставки до готовности предметного контракта; запрет обработки и публикации сохраняется

## Контекст

После чистого старта архитектура Deep Screening была описана, но в интерфейсе не существовало исполняемого представления нового сервиса. Полное ожидание Kafka-контракта, БД и Qwen скрывало фактическое состояние работ и не давало оператору отличить отсутствующую реализацию от пустой очереди или остановленной обработки. При этом создание фиктивных заданий, результатов или готовности модели нарушило бы fail-closed границу.

## Решение

Deep Screening получает первый самостоятельный read-only срез: owner-local HTTP API, React UI, Compose-профиль `ui`, технические `/healthz` и `/readyz` и Prometheus `/metrics`. Интерфейс восстанавливает из retained Kafka topics действующую проекцию `AdvanceScreeningSiteAccepted` с учётом отзывов, показывает её как ещё не обработанную входящую очередь, пустую поверхность сенсора, отдельное размещение Qwen и точные незакрытые ворота. Нулевыми остаются только фактически отсутствующие результаты Deep Screening; входной счётчик не подменяется статической заглушкой.

API не принимает предметные или операторские команды, читает из Kafka только retained topics принятия и отзыва Advance без commit offsets, не обращается к PostgreSQL или Web, не вызывает модель и не публикует сообщения. Любая роль обработки продолжает закрыто завершаться с `QWEN_EXPERT_RUNTIME_NOT_IMPLEMENTED`. Техническая готовность API/UI не меняет продуктовый статус `IN_PROGRESS` и не является готовностью Deep Screening.

Первый срез публикуется только на loopback, показывает публичные адреса из действующих принятий Advance и не содержит секретов, команд, решений или собственных результатов Deep, поэтому собственный OIDC-клиент не создаётся до появления защищаемого состояния. До добавления устойчивого inbox, решений или Process Control обязательны backend-проверка собственного audience, OIDC/PKCE UI и аудит команд по тем же механизмам, что Advance Screening.

Корневая композиция включает owner-local `compose.yml`, но обе роли имеют профиль `ui`; обычный запуск без профиля не активирует Deep Screening. Физические контейнеры используют читаемые имена `aelev_discovery_engine_deep_screening_api` и `aelev_discovery_engine_deep_screening_ui`.

## Последствия

- Deep Screening становится видимым и проверяемым до готовности предметной обработки;
- интерфейс не скрывает отсутствие устойчивого inbox/worker, Qwen и выходного контракта;
- запуск профиля `ui` выполняет только read-only Kafka-проекцию и безопасен относительно Web и канонических данных;
- следующий этап расширяет существующую owner-local поставку, а не возвращает прежнюю оболочку.

## Критерии проверки

- самостоятельный и общий Compose дают одинаковые модели двух read-only ролей;
- UI показывает `IN_PROGRESS`, `PAUSED`, фактические действующие принятия Advance, нулевые результаты Deep и закрытые ворота;
- POST и роли обработки закрыто запрещены;
- `/metrics` сообщает `processing_active 0` и фактическую глубину входной Kafka-проекции;
- без профиля `ui` Deep Screening не запускается;
- исходники, образ и build context не зависят от каталога соседнего сервиса.

## Связанные документы

- [Предмет Deep Screening](../20261026-0158-deep-screening-sensor-surface.md)
- [Самостоятельные сервисы Discovery](https://github.com/sapogov1978/discovery-research-platform/blob/8d1a663319ac15d8729cf1422858f37d57096c75/docs/architecture/05-discovery-services.md)
- [Спецификация Deep Screening](https://github.com/sapogov1978/discovery-research-platform/blob/8d1a663319ac15d8729cf1422858f37d57096c75/services/discovery-engine/deep-screening/SPEC.md)
- [Руководство оператора](https://github.com/sapogov1978/discovery-research-platform/blob/8d1a663319ac15d8729cf1422858f37d57096c75/services/discovery-engine/deep-screening/USER_MANUAL.md)
