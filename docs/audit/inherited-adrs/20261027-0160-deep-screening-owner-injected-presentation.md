# ADR-0160: Deep Screening самостоятельно инъектирует интерфейс, состояние и метрики

> **Происхождение standalone-редакции.** Источник: `docs/adr/20261027-0160-deep-screening-owner-injected-presentation.md`; SHA-256: `17b48b1acd66a1e20f52b12795c9ef74f4ed86c989cfaea02f896d4efe507039`; основание включения: прямая ссылка из поставки. Сохранены номер, статус, история замены и применимые решения; упоминания исключённых bounded contexts нейтрализованы без переноса их контрактов.

- **Статус:** Частично заменён [ADR-0162](20261027-0162-deep-screening-operator-presentation.md) в представлении предметного результата и [ADR-0168](20261028-0168-deep-screening-durable-sensor-surface-pipeline.md) в отсутствии предметного runtime и Process Control; owner-controlled инъекция, состояние компонентов и доставка метрик сохраняются
- **Дата:** 2026-10-27
- **Основание:** A5, A10, A12, ADR-0082, ADR-0100, ADR-0101, ADR-0102, ADR-0104, ADR-0152
- **Частично заменяет (Supersedes):** [ADR-0159](obsolete/20261027-0159-deep-screening-visible-read-only-slice.md) в составе первого исполняемого среза и критериях двух ролей; последующий ADR-0168 заменил его read-only предметную границу

## Контекст

Первый видимый срез ADR-0159 запускал самостоятельные API и UI, но не публиковал раздел в универсальную оболочку, не публиковал собственное техническое состояние и не доставлял метрики через owner-local устойчивый sender. Наличие адреса UI не делает сервис видимым в Platform UI: оболочка не знает участников и не обнаруживает контейнеры. Это расходилось с уже принятыми общими механизмами и явным требованием, что Deep Screening должен действовать как прежние самостоятельные сервисы.

## Решение

Deep Screening самостоятельно публикует в Platform UI версионированное описание функционального модуля и состояние собственных компонентов через отдельную identity и устойчивый owner-local outbox. Его UI исполняется на собственном origin в ограниченном iframe, реализует `AELEV_UI_READY`, принимает только точный `AELEV_UI_CONTEXT`, закрепляет CSS оболочки по digest и сообщает `AELEV_UI_REFRESH`. Platform UI не получает предметные данные или команды и не содержит имя Deep Screening в собственном каталоге.

Deep Screening самостоятельно собирает `/metrics` своим Prometheus внутри `deep-screening-telemetry-sender`, устойчиво журналирует передачу и отправляет метрики центральному инфраструктурному delivery по mTLS. Журнал, сертификаты, конфигурация, volume и внешняя метка `owner_id=deep-screening` принадлежат Deep Screening. Центральные delivery, Platform UI и Discovery Engine не перечисляют и не настраивают этот сервис.

Первый профиль `ui` содержит API, UI, status publisher и telemetry sender; одноразовый профиль `provision` содержит только подготовку собственной publisher identity и собственного telemetry volume. Состояние API/UI не утверждает готовность предметного runtime: Qwen, устойчивый inbox/worker и `SensorSurfacePackage` остаются не реализованы, обработка остаётся `PAUSED`.

## Последствия

- новый функциональный сервис появляется в оболочке без изменения её исходников, env или образа;
- состояние и метрики имеют owner identity, происхождение и устойчивую доставку;
- отказ оболочки или центральной телеметрии не передаёт им владение Deep Screening и не включает обработку;
- поставка получает дополнительные status publisher, telemetry sender, два owner-local volume и отдельные удостоверения.

## Критерии проверки

- исходники Platform UI и инфраструктурной композиции не содержат регистрации Deep Screening;
- status publisher публикует только свой module UUID, UI URL и пробы своих API/UI;
- iframe принимает сообщения только от точного origin оболочки и не получает её token;
- telemetry sender собирает только собственный `/metrics`, использует `owner_id=deep-screening` и сохраняет журнал при недоступности delivery;
- Platform UI показывает раздел и свежее состояние, а центральный Prometheus получает свежую серию `up{owner_id="deep-screening"}`;
- без Qwen и выходного контракта обработка остаётся выключенной.

## Миграция и откат

Установщик создаёт отдельные publisher и telemetry identities и новые owner-local volumes; существующие API/UI и read-only Kafka-проекция сохраняются. Откат останавливает status publisher и telemetry sender и выполняет штатную отписку состояния, но не удаляет их outbox, журнал, удостоверения или данные. Platform UI, центральный delivery и соседние сервисы не изменяются.

## Связанные документы

- [Контракт инъекции Platform UI](../api/platform-ui-injection.md)
- [Контракт телеметрии](../api/telemetry.md)
- [Спецификация Deep Screening](https://github.com/sapogov1978/discovery-research-platform/blob/8d1a663319ac15d8729cf1422858f37d57096c75/services/discovery-engine/deep-screening/SPEC.md)
- [Руководство Deep Screening](https://github.com/sapogov1978/discovery-research-platform/blob/8d1a663319ac15d8729cf1422858f37d57096c75/services/discovery-engine/deep-screening/USER_MANUAL.md)
