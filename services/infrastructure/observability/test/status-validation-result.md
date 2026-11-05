# Проверка независимой публикации продуктов observability

Дата: 2026-10-10. Итог: PASS для перечисленных локальных проверок.

Нативные регрессии: `docker run --rm -v /var/run/docker.sock:/var/run/docker.sock aelev/observability:checks pytest -q test` — 58 PASS. Docker socket доступен только временной операторской задаче проверки конфигурации, никогда рабочему издателю. Black/Ruff и строгий MyPy `prepare.py initialize.py delivery.py` — PASS. Добавлена регрессия независимых UUID/образов/журналов семи продуктов и исключения вложенных рабочих env/секретов из контекста сборки; проверочный образ не содержит `secrets` или `.env` частей.

| Собственный владелец строки | Модульные пробы | Реальный процесс и издатель, API/DOM, свежие публикации остальных |
|---|---|---|
| [delivery](../delivery/test/status-validation-result.md) | 6 PASS | PASS |
| [OpenTelemetry Collector](../otel-collector/test/status-validation-result.md) | 6 PASS | PASS |
| [Prometheus](../prometheus/test/status-validation-result.md) | 6 PASS | PASS |
| [Blackbox Exporter](../blackbox-exporter/test/status-validation-result.md) | 6 PASS | PASS |
| [Loki](../loki/test/status-validation-result.md) | 6 PASS | PASS |
| [Tempo](../tempo/test/status-validation-result.md) | 6 PASS | PASS |
| [Grafana](../grafana/test/status-validation-result.md) | 6 PASS | PASS |

Это указатель на отдельные сервисные отчёты, а не один общий тест или издатель observability. Каждый браузерный тест живёт в своём `<product>/test` и восстанавливает только свои исходные контейнеры. Рабочая поставка оставлена включённой для проверки в UI; постоянные тома, удостоверения и журналы не уничтожаются. Одноразовые выдачи собственных publisher identities и инициализация собственных томов выполнены с `run --rm`.

Нативная топология телеметрии и канонические контракты не изменены. Продукционная PKI/SVID, межстрановое размещение, нагрузка и восстановление после утраты узла этим локальным прогоном не подтверждены. Полный корневой Compose не запущен: его прежние прикладные параметры не подготовлены; статическая структура YAML проверена отдельно. Старые отчёты о телеметрии не заменены этим результатом.
