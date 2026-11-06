# 00-INFRA-20261106-0013-owner-certificate-lifecycle — Владельческий жизненный цикл сертификатов

- **Статус:** Принят
- **Дата:** 2026-11-06
- **Происхождение:** неизменяемые ADR-0165 и ADR-0166 из [архива выделения](../audit/inherited-adrs/INDEX.md)

## Контекст

Автоматическая ротация leaf certificates должна сохранять владельца private key, доверие issuer и атомарность замены без центрального писателя во все secret roots.

## Ключевые решения

- Каждый сервис владеет CA/leaf policy, private keys, approval, renewal state и reload/restart своего процесса.
- Общая delivery capability переносит только явно одобренные публичные leaf/issuer/report в точный owner destination.
- Private keys, approval secrets и административные credentials не покидают owner-local secret root.
- Leaf и key заменяются атомарно; автоматически обновляемые файлы монтируются owner-directory или Docker volume, а не отдельным file bind.
- Overlap bundle допускается только как ограниченный согласованный переход issuer; отзыв не восстанавливается автоматикой.
- Готовность проверяет фактические hashes, issuer/SAN/EKU, срок, свежую receipt и успешный reload/restart.
- CA и leaf содержат RFC 5280 extensions, включая SKI/AKI, необходимые строгой path validation.

## Последствия

- Нет общего root-процесса с доступом ко всем private keys.
- Ротация проверяема и обратима в пределах overlap без разрыва владельческой границы.
- Копирование нового файла само по себе не считается применением сертификата процессом.

## Проверка и откат

Certificate lifecycle gate закрыто проверяет owner state, timer, receipt, hashes и expiry. Negative tests покрывают wrong issuer/SAN/EKU, stale receipt, revoked state и detached bind mount. Откат выбирает ранее одобренный ещё действующий bundle и не возвращает отозванный leaf.

## Официальные основания

- [RFC 5280](https://www.rfc-editor.org/rfc/rfc5280.html) определяет X.509 certificate path, SKI/AKI, SAN, key usage и extensions.
- [SPIFFE Workload API](https://spiffe.io/docs/latest/spiffe-specs/spiffe_workload_api/) определяет обновляемые SVID и trust bundles.
- [Docker bind mounts](https://docs.docker.com/engine/storage/bind-mounts/) описывает inode/mount boundary, существенную для атомарной замены файлов.
