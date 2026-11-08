# Повторная сверка и критерии допуска 1.0.0

Сверка проведена 2026-11-08 для самостоятельного Platform Infrastructure. Поддерживаемая область — `full-local` из 15 инфраструктурных владельцев. Последняя предшествующая доказанная программная база — [b1e2ee1](clean-room-b1e2ee1/RESULT.md), полный gate 11/11 и CI 26/26. Её evidence не считается evidence нового 1.0.0.

## Несоответствия и исправления

| Проверка | Найденное несоответствие | Исправление и защита |
|---|---|---|
| Самостоятельная документация | Корневые и владельческие entrypoints считали документы потребителя нормативной архитектурой | Собственные [deployment](../architecture/deployment.md), [storage](../architecture/storage.md), [Platform UI](../architecture/platform-ui.md) и [supply chain](../security/supply-chain.md), локальные ссылки |
| Каталог API | Чужие семейства событий, модельные ячейки и routes оставались в публичном API инфраструктуры | Активные документы описывают только принятые технические контракты; [исходные байты](pre-1.0-api-documents.tar.gz) и [SHA-256](pre-1.0-api-documents.json) сохранены |
| SDK и история | Историческая документация сохранённого SDK могла восприниматься как текущий общий контракт | [Нейтральная точка входа](../../platform-ui/SDK.md); два прежних документа защищены [описью digest](frozen-sdk-documents.json), published package/CSS не изменены |
| Presence | Краткий UI-каталог смешивал heartbeat loss с withdrawal | Каталог согласован с каноническим injection-контрактом: LOST сохраняет присутствующую строку; withdrawal скрывает её |
| Семантика диаграмм | Корневая схема сохраняла конкретную модель/worker и старые ADR; PostgreSQL не уточнял `ANY 1` | Общая и Translator-схемы исключают обязательный provider; PostgreSQL показывает strict quorum и исполнение fencing под native-арендой; gate отвергает прежние model/layout references |
| Официальные источники | Одна ссылка SPIRE federation возвращала 404 | Адрес исправлен на официальный `/architecture/federation/readme/`; Grafana data sources подтверждён отдельно после сетевого отказа |
| Release metadata | Корень и full-local продолжали объявлять 0.2.0 | Оба manifest согласованы на 1.0.0; тесты текущего evidence используют текущую версию, исторические snapshots не меняются |
| Релизный процесс | Условия публикации и состав доказательств не были сведены в одну операционную цепочку | [Процедура release](../operations/release.md): exact commit, CI, новый полный gate, verifier, ZIP/lock, проверка assets и только затем публикация |

Дополнительный documentation gate воспроизвёл эти нарушения до исправления. Он запрещает ссылки активных entrypoints на нормативные документы потребителя и предметные семейства в `docs/api`, проверяет неизменность исключённых SDK-документов и наличие текущего руководства. Негативные регрессии проверяют различие действующих контрактов и исторических свидетельств. Наличие 15 комплектов README/SPEC/USER_MANUAL/INDEX/diagram и 25 официальных разделов остаётся отдельной обязательной проверкой; локальная связанность проверяется link gate.

Упомянутые исправления не меняют принятый стек, Kafka schemas, service ownership, образы опубликованных артефактов, данные, identities или журналы. Семантика инфраструктурного выпуска 1.0.0 не изменяет API установки v1 и формат source bundle v2.

## Доказательства опубликованного выпуска

Статус стабильного 1.0.0 подтверждается только [release-admission.json](https://github.com/sapogov1978/AELEV-platform-infrastructure/releases/download/v1.0.0/release-admission.json), приложенным к [release v1.0.0](https://github.com/sapogov1978/AELEV-platform-infrastructure/releases/tag/v1.0.0). Он должен содержать точный source commit, version 1.0.0, успешные CI и full-local run IDs, независимый verification и digest ZIP/lock/BOM/evidence. Отсутствующий asset, другой source/version, неполный gate или неуспешный verifier запрещают вывод о допуске 1.0.0 и публикацию stable tag.

ZIP извлекается отдельно от рабочего checkout; опись сверяется по всем bytes/text digests и executable modes, после чего выполняется настоящий установочный `plan` без старта контейнеров. Source bundle не содержит локальные секреты, env, runtime, Graphify или агентские навыки. Evidence включает реальные функциональные артефакты, а не только итоговый JSON или status badge.

## Пределы вывода

Это stable source release поддерживаемого локального инфраструктурного продукта. Допуск конкретной production-площадки, подписанные готовые runtime images, WAN/failure domains, PKI/KMS/SVID, capacity, независимый backup/PITR, RPO/RTO и alert delivery проверяются отдельно. Выбор реализации перевода остаётся открытым: его внутренности не принадлежат Translator, обязательная приёмка проверяет сохранность его очереди без провайдера.
