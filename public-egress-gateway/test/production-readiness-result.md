# Проверка готовности Public Egress Gateway

Дата: 2026-10-14.

Результат продукта: `PASS`.

Изолированный Docker test stage собран только из каталога сервиса. В нём прошли 62 теста, `ruff check .` и строгий Pyright без ошибок и предупреждений. Compose со всеми профилями успешно разрешён из `.env.example`.

Проверки покрывают запрет изменения grant при невозможности сохранить audit intent, последовательные `INTENT_RECORDED`/`MUTATION_COMMITTED`, целостность и обнаружение подмены хеш-цепочки, обязательность HTTPS/mTLS telemetry endpoint в production, необязательность телеметрии только в local и продолжение входного `traceparent`. Поставка содержит собственные Prometheus rules/configuration, постоянный journal telemetry sender и раздельные client/sender identities.

Не выполнялись и не объявляются выполненными: WAN/default-deny конкретной площадки, реальная SPIRE-ротация, подпись configuration artifact, приём локального источника каноническим `security-audit-collector`, доставка alert в целевой канал и rollout реальных потребителей. По реестру готовности это допуски внешнего развёртывания; локальный источник аудита и интеграционная точка поставки реализованы.
