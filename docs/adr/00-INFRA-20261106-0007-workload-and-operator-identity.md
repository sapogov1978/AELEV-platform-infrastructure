# 00-INFRA-20261106-0007-workload-and-operator-identity — Удостоверения workloads и операторов

- **Статус:** Принят
- **Дата:** 2026-11-06
- **Происхождение:** неизменяемые ADR-0017, ADR-0108, ADR-0111, ADR-0117, ADR-0119 и ADR-0154 из [архива выделения](../audit/inherited-adrs/INDEX.md)

## Контекст

Machine-to-machine и человеческий доступ имеют разные угрозы и не должны использовать общие bearer credentials или браузерные клиентские ключи.

## Ключевые решения

- SPIRE выдаёт workload identities через стандартный SPIFFE Workload API; workloads не получают административный API SPIRE.
- Защищённые технические endpoints используют TLS/mTLS и отдельные identity каждого владельца.
- Человеческий вход централизован в Keycloak, но каждый UI владеет собственным OIDC client, audience, callback и ролями.
- Browser clients используют Authorization Code с PKCE; Platform UI не передаёт собственный token внедрённому UI.
- Обычная локальная форма, Basic Auth и anonymous access отключены; bootstrap credential допускается только как явно ограниченный recovery-механизм.
- HTTP разрешён только для опубликованного loopback local-test endpoint; любой не-loopback операторский endpoint использует HTTPS.
- Секреты находятся в owner-local roots, не входят в Git/образ и монтируются минимально необходимой роли.

## Последствия

- Компрометация одного client или workload не предоставляет полномочия соседнего владельца.
- Единая SSO-сессия не создаёт общий OAuth client или общий token.
- Production PKI и trust domains допускаются площадкой отдельно от локальных fixtures.

## Проверка и откат

Negative tests отклоняют неверные SAN, issuer, audience, role, callback и non-loopback HTTP. Secret scan и container-security gate запрещают встроенные credentials. Откат identity-конфигурации сохраняет прежние ключи до подтверждённой перекрывающей ротации.

## Официальные основания

- [SPIFFE Workload API](https://spiffe.io/docs/latest/spiffe-specs/spiffe_workload_api/) определяет выдачу и обновление workload identity.
- [Keycloak securing applications](https://www.keycloak.org/securing-apps/overview) описывает OIDC/OAuth2-интеграцию приложений.
- [OpenID Connect Core](https://openid.net/specs/openid-connect-core-1_0.html), [RFC 7636 PKCE](https://www.rfc-editor.org/rfc/rfc7636.html) и [RFC 8705 mTLS](https://www.rfc-editor.org/rfc/rfc8705.html) задают применённые механизмы аутентификации.
