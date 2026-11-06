# Модули Platform UI

Platform UI является технической оболочкой навигации, статуса и presentation context. Он не содержит статический список функциональных сервисов и не становится владельцем их состояния, команд, ролей или данных.

## Декларация владельца

Владелец публикует версионированную декларацию с `module_id`, `service_kind`, локализованным заголовком, owner URL, секциями, порядком, `status_sequence`, временем записи и происхождением. Декларация принимается только от точной machine identity владельца; повтор одной версии идемпотентен, регресс sequence и другое содержимое под той же identity отклоняются.

`service_kind=FUNCTIONAL` и хотя бы одна section разрешают отображение в основной навигации. Infrastructure и status-only modules остаются на системной поверхности. Порядок является только presentation metadata и не передаёт полномочия.

## Собственный UI

Каждый модуль остаётся самостоятельным web-приложением с собственным origin, OIDC client, ролями и сессией. Shell передаёт только язык, разрешённый stylesheet URL/digest, bounded refresh state и версию bridge protocol. Access token, credentials и предметные команды через iframe bridge не передаются.

Получатель `postMessage` проверяет точные `origin`, `source`, protocol version, message type и разрешённые поля. Неизвестное поле, неверный origin или несовместимая версия закрыто отклоняются без изменения UI владельца.

## Живость и отзыв

Status publisher сначала устойчиво записывает owner-local declaration/heartbeat, затем отправляет его в оболочку. Отсутствующий, повреждённый, будущий или устаревший heartbeat не проходит локальную health-проверку. Локальная живость publisher не доказывает доставку оболочке.

Штатное снятие модуля публикует монотонный withdrawal с последней известной identity и sequence. Истечение heartbeat скрывает представление как недоступное, но не создаёт withdrawal и не удаляет историю владельца.

## Совместимость

Версия декларации и bridge protocol изменяются независимо. Добавление необязательного presentation field не меняет предметный API владельца; изменение origin, trust rule, required field или семантики команды требует новой major-версии. Общие SDK и stylesheet являются digest-pinned техническими артефактами и не содержат предметных моделей.

Нормативные детали публикации, browser boundary и refresh indicator определены в [контракте инъекции](platform-ui-injection.md), [архитектуре Platform UI](https://github.com/sapogov1978/discovery-research-platform/blob/8d1a663319ac15d8729cf1422858f37d57096c75/docs/architecture/22-platform-ui.md) и 00-INFRA-20261106-0008-platform-ui-owner-injection, 00-INFRA-20261106-0008-platform-ui-owner-injection, 00-INFRA-20261106-0008-platform-ui-owner-injection, 00-INFRA-20261106-0008-platform-ui-owner-injection, 00-INFRA-20261106-0008-platform-ui-owner-injection, ADR-0160 и ADR-0161.
