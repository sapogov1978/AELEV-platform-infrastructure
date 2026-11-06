# 00-INFRA-20261106-0008-platform-ui-owner-injection — Владельческая инъекция Platform UI

- **Статус:** Принят
- **Дата:** 2026-11-06
- **Происхождение:** неизменяемые ADR-0073, ADR-0095, ADR-0101, ADR-0102 и ADR-0105 из [архива выделения](../audit/inherited-adrs/INDEX.md)

## Контекст

Общая оболочка должна показывать техническое состояние самостоятельных владельцев, не получая их предметные данные, команды, credentials или порядок версий.

## Ключевые решения

- Каждый владелец публикует версионированное описание своего модуля, разделов, компонентов и технического состояния.
- Platform UI хранит read-only производную проекцию и собственный технический журнал приёмника; каноническое описание остаётся у издателя.
- UI владельца работает на отдельном origin, самостоятельно авторизуется и загружается в sandboxed iframe.
- Shell не проксирует предметные API и не передаёт token, client key или полномочия соседнему UI.
- Presence, graceful withdrawal, revision, sequence, UUID и freshness проверяются явно; отсутствие связи не подменяется штатной отпиской.
- Порядок отображения объявляет владелец через контракт, а не сортировка frontend по имени.
- Общий refresh indicator отражает только состояние текущего открытого раздела и не становится глобальной бизнес-готовностью.

## Последствия

- Новые инфраструктурные owners подключаются без изменения каталога приложений в оболочке.
- Потеря или восстановление одного publisher не изменяет состояние других.
- Резервная копия проекции нужна отдельно: повторная публикация не восстанавливает удалённую историю и tombstones.

## Проверка и откат

Contract tests проверяют schema, sandbox/CSP, origin, revision/sequence monotonicity, withdrawal и отрицательные permissions. Откат описания выполняется новой большей revision без сброса UUID, sequence или журнала.

## Официальные основания

- [HTML iframe specification](https://html.spec.whatwg.org/multipage/iframe-embed-object.html#the-iframe-element) определяет sandboxed browsing context.
- [Content Security Policy Level 3](https://www.w3.org/TR/CSP3/) определяет ограничения origins и embedded content.
- [OpenID Connect Core](https://openid.net/specs/openid-connect-core-1_0.html) и [RFC 7636](https://www.rfc-editor.org/rfc/rfc7636.html) описывают независимую авторизацию browser clients.
