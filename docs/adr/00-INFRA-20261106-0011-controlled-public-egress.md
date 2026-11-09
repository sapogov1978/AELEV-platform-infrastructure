# 00-INFRA-20261106-0011-controlled-public-egress — Контролируемый публичный исходящий доступ

- **Статус:** Принят
- **Дата:** 2026-11-06
- **Происхождение:** неизменяемые ADR-0074 и ADR-0114 из [архива выделения](../audit/inherited-adrs/INDEX.md)
- **Исключение:** runtime внешнего opaque provider по 00-INFRA-20261109-0016

## Контекст

Инфраструктура должна предоставлять ограниченный доступ к публичным HTTP(S)-ресурсам, не принимая предметных решений и не выдавая потребителю общие сетевые credentials.

## Ключевые решения

- Public Egress Gateway является самостоятельным инфраструктурным владельцем generic fetch capability.
- Каждый запрос требует точной workload identity, активного owner-scoped grant, разрешённых scheme/host/port и ограниченных метода, размера, времени и redirects.
- Gateway передаёт публичные bytes и технические metadata, но не хранит канонические документы, не классифицирует ответ и не выбирает следующий адрес.
- DNS resolution, connect destination и certificate SAN перепроверяются против политики; loopback, private, link-local и запрещённые ranges блокируются.
- Grants принадлежат потребителю и не создаются корневой композицией.
- Health endpoint не является доказательством действующего grant или успешного доступа к конкретному назначению.

## Последствия

- Исходящий доступ централизован как техническая capability без централизации предметной логики.
- Компрометация одного grant не открывает произвольный proxy.
- Перед production-нагрузкой требуется bounded smoke exact identity/grant/destination.

## Проверка и откат

Negative tests покрывают SSRF ranges, DNS rebinding, redirects, SAN, expiry, method и quota. Отзыв grant блокирует новые запросы без удаления consumer history; откат gateway не восстанавливает отозванные полномочия.

## Официальные основания

- [RFC 9110 HTTP semantics](https://www.rfc-editor.org/rfc/rfc9110.html) определяет методы, redirects и представление HTTP.
- [RFC 8705](https://www.rfc-editor.org/rfc/rfc8705.html) описывает mTLS client authentication и certificate binding.
- [SPIFFE Workload API](https://spiffe.io/docs/latest/spiffe-specs/spiffe_workload_api/) определяет выдачу workload identity без общего статического секрета.
