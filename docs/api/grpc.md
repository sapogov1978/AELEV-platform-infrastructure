# Технические gRPC и HTTPS-интерфейсы

Межсервисные предметные команды, запросы и события используют Kafka по [00-INFRA-20261106-0005-kafka-cloudevents-contracts](../adr/00-INFRA-20261106-0005-kafka-cloudevents-contracts.md). Этот каталог не объявляет RPC, модельные или прикладные API потребителя.

| Техническая поверхность | Владелец и контракт | Ограничение |
|---|---|---|
| SPIRE Server/Agent и SPIFFE Workload API | [Workload identity](workload-identity.md), [SPIRE SPEC](../../spire/SPEC.md) | Стандартные протоколы удостоверений; административные полномочия не выдаются потребителю |
| OTLP | [Telemetry](telemetry.md), [Collector](../../observability/otel-collector/SPEC.md) | Только техническая телеметрия; устойчивый межузловой вход определяется HTTPS-квитанциями владельца |
| Публичный fetch | [Public Egress Gateway](../../public-egress-gateway/SPEC.md) | Собственный HTTPS/mTLS `/v1/fetch`, точный grant, без предметного решения |
| Операторский browser UI | [Platform UI injection](platform-ui-injection.md) | Собственные HTTPS/OIDC API и ограниченный browser bridge; без передачи токенов |
| Хранение и пробы | SPEC соответствующего владельца и [health/readiness](health-readiness.md) | Нативные технические протоколы, собственные данные и состояние |

Внутренний протокол одного владельца не создаёт общего интерфейса к чужой БД, сокету, модели или журналу. Translator пользуется только [непрозрачным портом исполнения](model-compute.md); способ перевода и провайдер не определяются этим документом. Новое предметное межсервисное RPC требует отдельного заменяющего ADR, а не дописывания маршрутов в эту таблицу.

## Официальные источники

- [SPIFFE concepts и Workload API](https://spiffe.io/docs/latest/spiffe-about/spiffe-concepts/).
- [OTLP specification](https://opentelemetry.io/docs/specs/otlp/).
- [Kafka documentation](https://kafka.apache.org/39/documentation/).

Прежняя ошибочно перенесённая редакция сохранена побайтно в [аудиторском архиве](../audit/pre-1.0-api-documents.tar.gz) с [описью](../audit/pre-1.0-api-documents.json).
