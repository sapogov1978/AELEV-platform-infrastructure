# Задачи инфраструктурной оркестрации

## Локальный кандидат 1.1.8 — 18 ноября 2026

- [x] Проверить MinIO lifecycle source `2d597d8` с сохранением прежнего image tag и точного Docker baseline.
- [x] Выполнить полный clean-room того же функционального source: [12/12 функций, независимый verifier PASS](docs/audit/full-local-2d597d8-20261118.md).
- [ ] Выполнить новый exact-source remote audit и публикацию после снятия ограничения local-only; текущий отчёт не заменяет эти gates и не допускает production-площадку.

## Clean-room full-local

- [x] Зафиксировать полный состав, внешние входы и функции приёмки в `full-local.json`.
- [x] Добавить fail-closed preflight, source/image BOM и verifier неизменяемого evidence.
- [x] Добавить единый runbook для инженера без знания истории репозитория.
- [x] Реализовать единый исполняемый gate, который сам запускает двенадцать реальных функций, формирует evidence и проверяет cleanup.
- [x] Включить выбранную provider/worker-ячейку в root/full-local только как два подписанных digest-pinned image с opaque UDS и generic secret mount; не переносить provider ownership в Translator или Platform UI.
- [x] Проверять реальным Kafka/PostgreSQL lifecycle, что Translator принимает, дедуплицирует и сохраняет обе очереди без выбранного провайдера.
- [ ] Отдельно выбрать локальный runtime, образ или внешний API провайдера после сравнения стоимости, безопасности, качества и эксплуатационных ограничений; это решение не блокирует готовность данного репозитория.
- [x] Выполнить полный clean-room run, сохранить проверяемый пакет 11/11 функций и повторить [Production Ready аудит](docs/audit/production-readiness.md) программной версии `b7a6b4b`; каждый следующий release commit допускается только после собственного CI/gate.
- [x] Закрыть вновь воспроизведённый pair-loss timeout: [исправленная версия `b1e2ee1`](docs/audit/clean-room-b1e2ee1/RESULT.md) прошла initial-1 3/3 без switchover, root/Kafka/initial-1 и новый полный gate 11/11 с независимой проверкой.
- [x] Выполнить полный gate 12/12 и независимый verifier для release 1.1.0 с exact provider/worker digests.
- [ ] Повторить полный gate 12/12 и независимый verifier для patch-release 1.1.1 с исправленным strict X.509 bootstrap; evidence 1.1.0 не переносится на новый source commit.

Владельческие незавершённые работы по-прежнему ведутся в `TASKS.md` соответствующего продукта и здесь не дублируются. Эти два пункта относятся к межвладельческой композиции и потому остаются в корневом списке.
