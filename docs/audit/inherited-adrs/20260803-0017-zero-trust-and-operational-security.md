# ADR-0017: Нулевое доверие и эксплуатационная безопасность

> **Происхождение standalone-редакции.** Источник: `docs/adr/20260803-0017-zero-trust-and-operational-security.md`; SHA-256: `7ce22f60ffa788dcb1fb9538609e2141d90fa51bc16aeb8d175acfbddff99359`; основание включения: прямая ссылка из поставки. Сохранены номер, статус, история замены и применимые решения; область решения адаптирована к самостоятельному продукту.

- **Статус:** Частично заменён [ADR-0091](20261008-0091-service-owned-kafka-topics.md) только в части владения созданием Kafka topic: издатель владеет декларацией и отдельной задачей развёртывания, а запрет `Create`/`Alter`/`Delete` для рабочего процесса и остальные требования этого ADR действуют
- **Дата:** 2026-08-03
- **Основание:** A5, A7, A8, A10, A12–A16
- **Заменяет:** —

## Контекст

Продукт читает внешние публичные ресурсы, принимает operator commands, запускает заменяемые вычислительные модули и передаёт устойчивые события между независимыми владельцами. Доверие только к сетевой границе, общие credentials или проверка лишь готового image не ограничивают последствия компрометации и не доказывают происхождение команды.

Модель безопасности должна охватывать все поставки без создания второго предметного транспорта, общего администратора данных или ручного обхода owner-local admission и recovery barriers.

## Критерии выбора

- отдельная проверяемая identity каждой workload и среды;
- минимальные полномочия и запрет по умолчанию;
- доказуемое происхождение команд, выпусков и административных действий;
- короткий срок credentials и ограниченная область отзыва;
- разделение human identity и machine identity;
- воспроизводимое восстановление из подписанных неизменяемых артефактов;
- отсутствие секретов в Git, image, Kafka, log, metric и UI.

## Рассмотренные альтернативы

1. Общие постоянные сертификаты или пароли. Отклонено: невозможно избирательно отозвать экземпляр и доказать его действия.
2. Только mTLS и сетевые правила. Отклонено: защищает канал, но не подтверждает допустимого издателя, поколение или сохранённую команду.
3. Одна identity для всей Compose-группы. Отклонено: группа не является владельцем и получает чрезмерные права.
4. Уникальные SPIFFE identities, точные ACL, signed immutable releases, отдельные human roles и неизменяемый аудит.

## Решение

### Идентичность workload и Kafka

Каждая среда имеет отдельный SPIFFE trust domain. Production workload получает короткоживущий X.509-SVID через SPIRE и проходит взаимную TLS-аутентификацию. Авторизация проверяет точный URI SAN, audience, роль, scope и expiry; доверия к CA без проверки identity недостаточно.

Kafka запрещает доступ по умолчанию. ACL разрешают точной identity запись только в topics владельца и чтение только перечисленных входов собственной consumer group. Runtime не получает `Create`, `Alter`, `Delete` для topics или ACL. Topic declaration и отдельная идемпотентная provisioning job принадлежат владельцу контракта по ADR-0091.

Human identity управляется отдельно через Keycloak/OIDC. Browser token не используется как machine credential. Человек не публикует предметное событие непосредственно в Kafka и не получает service credential через UI.

### Секреты и ключи

Секрет доставляется из одобренного secret-management механизма только после проверки identity и только в память либо защищённый read-only file mount. Он не помещается в переменную Compose, command line, image layer, Git, log, trace, metric, response или diagnostic bundle.

Credentials разделены по среде, сервису и назначению. Временная provisioning identity не используется runtime. Ротация проверяет новый credential до отзыва прежнего; чтение, изменение и отзыв журналируются без значения секрета. Закрытый ключ с правами group или others закрыто отклоняется.

### Сетевые границы

Public egress разрешён только через Public Egress Gateway под точной workload identity и active exact-scope grant. Gateway повторно разрешает DNS и блокирует loopback, link-local, private, metadata, cluster и иные special-use destinations, включая DNS rebinding.

Database, Kafka, object storage и owner APIs вне loopback/local profile используют TLS с проверкой имени и цепочки. Внутренняя сеть Compose не является основанием доверия. Сервис не получает Docker socket, host privileges или network namespace соседнего владельца.

### Артефакты и вычислительные модули

В production запускается точный immutable image или artifact по digest. Поставка закрепляет происхождение сборки, dependency lock, перечень компонентов, подпись и совместимость. Изменяемый tag не является admission evidence.

Owner-internal compute module получает только минимальный request contract. Provider credential остаётся внутри модуля; функциональный runtime не знает и не передаёт его. Неизвестный release, schema mismatch, stale attestation или timeout после возможной отправки закрыто блокируют продвижение и не разрешают blind retry.

### Полномочия оператора

Оператор может наблюдать, приостанавливать новую работу, запускать проверяемое восстановление, продвигать заранее проверенный выпуск и отзывать доступ в пределах своей роли. Команда `PAUSE`, `RESUME`, `RESET`, release switch либо recovery handoff содержит expected revision, actor, reason и stable command id.

UI не предоставляет скрытый обход Kafka, policy gate, Process Control или recovery history. Аварийная роль отделена от обычной, ограничена по времени, требует усиленной аутентификации и не изменяет канонические данные напрямую.

### Аудит и телеметрия

Security-relevant действие создаёт bounded structured record с actor/workload identity, действием, результатом, resource identity, environment, временем, correlation/causation и digest относящейся команды либо артефакта. Секреты, полный документ, policy text, arbitrary exception и user input не записываются.

Доказательный аудит отделён от обычной телеметрии. Сервисные logs, metrics и traces остаются owner-local projections; central observability предоставляет только хранение и query capability и не становится владельцем предметной семантики.

### Инцидент и восстановление

Инцидент проходит регистрацию, оценку области, изоляцию, сохранение доказательств, отзыв identity и credentials, сверку owner-local state, восстановление из проверенного backup/artifact и явный возврат. Неизвестное внешнее состояние либо несогласованный inbox/outbox блокируют несовместимый retry.

Возврат требует устранения причины, новой identity, проверенного выпуска, проверки журналов и воспроизведения затронутого диапазона. История, tombstones, command generations и использованные evidence не удаляются для получения зелёного статуса.

## Последствия

Положительные:

- компрометация одной identity или среды имеет ограниченную область;
- издателя, команду и release можно проверить независимо от текущего канала;
- оператор не становится вторым каноническим писателем;
- расследование и восстановление опираются на неизменяемые доказательства.

Отрицательные:

- SPIRE, точные ACL, lifecycle credentials и supply-chain admission увеличивают эксплуатационную сложность;
- индивидуальные identities требуют автоматического выпуска и отзыва;
- fail-closed проверки создают явные средовые gates перед production.

## Критерии проверки

- workload без допустимого X.509-SVID или с identity другой среды не соединяется с production dependency;
- ACL-тесты запрещают неразрешённые topic, operation и consumer group;
- неверная, устаревшая или отправленная неуполномоченной identity команда не меняет состояние;
- secret отсутствует в tracked files, images, events, logs и постоянной конфигурации;
- gateway блокирует special-use destinations и DNS rebinding;
- admission отклоняет неподписанный artifact и изменённый digest;
- UI и operator API не могут обойти owner-local revision и audit;
- backup/restore сохраняет identities references, inbox/outbox, generations и tombstones.

## Миграция и откат

Сначала вводятся раздельные identities, точные Kafka ACL, secret delivery, immutable release digests и bounded audit. Изменения event envelopes вводятся совместимо по ADR-0005. Production activation запрещена до проверки exact identities, ACL, digest, backup/restore и owner-specific readiness.

Откат возвращает предыдущие подписанные artifact, configuration и ACL templates. Он не отключает проверку identity, provenance, digest или аудит. Если безопасный откат невозможен, новая работа остаётся заблокированной до сверки.

## Связанные документы

- [Обзор системы](https://github.com/sapogov1978/discovery-research-platform/blob/8d1a663319ac15d8729cf1422858f37d57096c75/docs/03-system-overview.md)
- [Внутренние события](../api/internal-events.md)
- [Идентичность рабочих нагрузок](../api/workload-identity.md)
- [Развёртывание](https://github.com/sapogov1978/discovery-research-platform/blob/8d1a663319ac15d8729cf1422858f37d57096c75/docs/architecture/21-deployment.md)
- [ADR-0001](20260727-0001-kafka-only.md)
- [ADR-0005](20260727-0005-event-contract-governance.md)
- [ADR-0091](20261008-0091-service-owned-kafka-topics.md)
- [ADR-0114](20261012-0114-infrastructure-public-egress-gateway.md)
- [ADR-0154](20261020-0154-spire-workload-identity-provider.md)
