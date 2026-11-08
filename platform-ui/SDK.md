# Текущий инфраструктурный контракт Platform UI SDK

Владелец SDK — Platform UI. Универсальная граница определяется [SPEC](SPEC.md), [архитектурой](../docs/architecture/platform-ui.md) и [контрактом инъекции](../docs/api/platform-ui-injection.md). Библиотека предоставляет техническое представление и browser-интеграцию, не каталог приложений, модели данных или предметный API.

| Поверхность | Источник | Владелец данных и полномочий |
|---|---|---|
| Экспорты и peer dependencies | [package.json](operator-ui-kit/package/package.json) | Отдельный закреплённый SDK; приложение собирает свой UI |
| `PlatformPage`, navigation и presentation | [PlatformPage](operator-ui-kit/package/src/PlatformPage.tsx), [presentation metadata](operator-ui-kit/package/src/presentation.ts) | Platform UI владеет каркасом и неизменяемым CSS; приложение передаёт содержимое |
| OIDC/PKCE и embedded runtime | [moduleAuth](operator-ui-kit/package/src/moduleAuth.ts), [moduleRuntime](operator-ui-kit/package/src/moduleRuntime.tsx) | Сессия и роли принадлежат приложению; bridge не переносит токены |
| Опрос, состояния процессов и очередей | [SDK exports](operator-ui-kit/package/package.json) и экспортированные TypeScript types | Владелец проверяет значения, endpoints, interval и смысл действий; библиотека не создаёт предметный контракт |

Сохранённые `QueueServiceApp`, `QueueCurrentState`, `QueueResultHero` и delivery-компоненты являются поверхностями совместимости существующего пакета. Их типы не объявляют инфраструктуру владельцем прикладных очередей, результатов или delivery API. Для нового приложения действуют его собственные API, schemas, roles и проверки; нельзя выводить их из примеров прежнего потребителя.

Пакет `@aelev/operator-ui-kit` 0.1.4 и опубликованный presentation не переупаковываются ради изменения документации. Его прежние [README](operator-ui-kit/package/README.md) и [SPEC](operator-ui-kit/package/SPEC.md) сохранены как историческая документация артефакта; ссылки и названия приложений в них не являются нормативными контрактами Platform Infrastructure. [Опись SHA-256](../docs/audit/frozen-sdk-documents.json) запрещает незаметное изменение этих байтов. Текущая нормативная точка входа — этот документ и контракты владельца выше.

Новая общая геометрия, поведение или interface требует новой версии SDK/presentation и совместимости потребителей. Эта документационная сверка не меняет package version, runtime, экспорты, CSS или принятые технологические версии.
