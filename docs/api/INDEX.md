# Публичные технические контракты: карта каталога

Эта агентская карта дополняет Graphify и отвечает на вопрос «что и где искать». Нормативные источники — контракты владельцев, их исполняемые schemas и [ключевые ADR](../adr/INDEX.md); каталог не содержит API конкретного приложения.

| Что искать | Где находится |
|---|---|
| Локальная установка, full-local и BOM | [installation.md](installation.md) |
| Kafka envelope и владельческие event families | [internal-events.md](internal-events.md) |
| Запрос и результат технического перевода | [translation.md](translation.md), [schema artifact](../../translator-service/contracts/translation-v1.json) |
| Непрозрачная граница исполнения перевода | [model-compute.md](model-compute.md) |
| Технические gRPC/HTTPS-протоколы и запрет предметного RPC | [grpc.md](grpc.md) |
| Workload identity и полномочия | [workload-identity.md](workload-identity.md) |
| UI injection, browser bridge и присутствие | [platform-ui-injection.md](platform-ui-injection.md), [platform-ui-modules.md](platform-ui-modules.md) |
| Доставка технической телеметрии | [telemetry.md](telemetry.md) |
| Живость и готовность | [health-readiness.md](health-readiness.md) |

Предметные topics, schemas, databases, endpoints и policies потребителя определяются в его собственной поставке. Предыдущие ошибочно перенесённые API-документы сохранены побайтно в [аудиторском архиве](../audit/pre-1.0-api-documents.tar.gz) с [описью SHA-256](../audit/pre-1.0-api-documents.json); они не являются активным API этого продукта.
