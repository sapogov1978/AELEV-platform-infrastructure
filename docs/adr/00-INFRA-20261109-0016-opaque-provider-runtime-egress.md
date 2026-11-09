# 00-INFRA-20261109-0016-opaque-provider-runtime-egress — Сеть внешнего непрозрачного провайдера

- **Статус:** Принят
- **Дата:** 2026-11-09
- **Уточняет:** 00-INFRA-20261106-0011 и 00-INFRA-20261106-0012

## Контекст

Выбранный Translation Provider поставляется отдельным immutable image и сам
владеет удалённым API, endpoint, model ID, prompt и credential. Передача его
запроса через generic fetch gateway потребовала бы раскрыть инфраструктуре
provider-specific transport и отдельные identity/grant, оставляя несколько
локальных блокеров вместо одного credential.

## Ключевые решения

- Необязательный `translator-service/compose.compute.yml` предоставляет только
  provider service исходящую Compose-сеть. Входящие ports и `expose` запрещены;
  worker продолжает обращаться только к opaque HTTP v1 через Unix socket.
- Provider image получает один generic read-only файл
  `/run/secrets/provider/credential.env`. Infrastructure не задаёт provider environment,
  model ID, endpoint, prompt, command, entrypoint или healthcheck.
- Этот прямой исходящий доступ является узким исключением для отдельного opaque
  provider runtime. Public Egress Gateway остаётся основной generic fetch
  capability по 00-INFRA-20261106-0011 и не ослабляется для других владельцев.
- Recipe не входит в `full-local`, release BOM или автоматический production
  admission. API key создаётся вручную вне Git; его отсутствие остаётся
  единственным локальным runtime-вводом после подготовки exact images и
  владельческих database/telemetry routes.

## Последствия

Infrastructure не получает implementation identity или provider credential, а
Translator contract и очередь не меняются. Компрометация provider image при этом
даёт ему более широкий исходящий HTTPS-доступ, чем exact gateway grant; риск
ограничивается отдельным image digest, отсутствием входящих ports, read-only
filesystem, dropped capabilities и независимым admission провайдера.

## Проверка и откат

Compose tests требуют разные digest-pinned provider/worker images, единственный
generic secret mount, отсутствие provider environment/command/entrypoint/
healthcheck и отсутствие входящих ports. Station validation fail-closed
повторяет эти условия. Откат возвращает прежний provider image или удаляет
необязательную compute-ячейку; он не удаляет Kafka jobs, checkpoints, UDS volume
или Translator database и не означает `RESUME`.

## Официальные основания

- [Docker Compose services](https://docs.docker.com/reference/compose-file/services/)
  описывает независимые service networks, bind mounts и отсутствие published
  ports по умолчанию.
- [Docker bind mounts](https://docs.docker.com/engine/storage/bind-mounts/)
  описывает read-only файловую доставку host secret в отдельный container.
- [RFC 9110](https://www.rfc-editor.org/rfc/rfc9110.html) определяет HTTPS request
  semantics, остающуюся внутренней ответственностью provider image.
