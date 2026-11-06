# Проверки слоя и самостоятельных продуктов наблюдения

Дата: 2026-10-11. Итог локальных проверок: PASS. Семь нативных продуктов и семь собственных издателей оставлены работающими из отдельных Compose.

| Владелец | Собственные регрессии | Реальная готовность и публикация |
|---|---|---|
| [loki](../loki/test/standalone-validation-result.md) | 16 PASS | [PASS](../loki/test/startup-result.md) |
| [tempo](../tempo/test/standalone-validation-result.md) | 16 PASS | [PASS](../tempo/test/startup-result.md) |
| [blackbox-exporter](../blackbox-exporter/test/standalone-validation-result.md) | 16 PASS | [PASS](../blackbox-exporter/test/startup-result.md) |
| [prometheus](../prometheus/test/standalone-validation-result.md) | 16 PASS | [PASS](../prometheus/test/startup-result.md) |
| [otel-collector](../otel-collector/test/standalone-validation-result.md) | 16 PASS | [PASS](../otel-collector/test/startup-result.md) |
| [delivery](../delivery/test/standalone-validation-result.md) | 50 PASS | [PASS](../delivery/test/startup-result.md) |
| [grafana](../grafana/test/standalone-validation-result.md) | 16 PASS | [PASS](../grafana/test/startup-result.md) |

146 сервисных регрессий, 16 проверок публичной библиотеки подготовки, 3 проверки слоя observability и 30 верхнеуровневых регрессий инфраструктуры — всего 195 PASS. Black/Ruff/строгие типы изменённого кода пройдены. Верхнеуровневые типы проверены с настоящими Python-типами закреплённого Playwright 1.56.0, без запуска его браузера/бинарников в Alpine; ОС/Python рабочих сервисов не менялись.

Точное сравнение всех разрешённых ролей, сетей и томов каждого самостоятельного продукта с включённой моделью слоя — PASS. Верхний инфраструктурный Compose разрешается с новыми моделями без запуска общей группы поверх отдельных писателей. Шесть настоящих init-задач завершились exit 0 и были удалены через run --rm; у Blackbox постоянного тома нет. Постоянные данные, identities, UUID и журналы не удалялись. Рабочие env/секреты не отслеживаются Git.

Реальные опубликованные HTTPS/mTLS маршруты, шесть источников технических метрик, семь readiness-проб и запросы Grafana к трём источникам — [отдельный читаемый результат](routes-result.md). Heartbeat издателя не считается квитанцией UI: собственная проверка запуска требует свежего подтверждённого события status_published с ONLINE.

Проверены 45 документов, 200 локальных ссылок, 21 ссылка на заголовок, баланс блоков кода, заголовок Mermaid и пробельные ошибки. Прежний связанный runtime и его заменённые инструкции/тесты находятся в obsolete. Алгоритм delivery после перемещения и форматирования имеет прежний Python AST.

Не проверялись новый полный stop/start всех компонентов, браузерный цикл, WAN, production PKI/ротация, нагрузка, независимое резервное восстановление и внешняя почта. SMTP явно выключен. Локальный PASS не закрывает эти ворота и не заменяет прежние исторические отчёты.
