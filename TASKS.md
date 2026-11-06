# Задачи инфраструктурной оркестрации

## Clean-room full-local

- [x] Зафиксировать полный состав, внешние входы и функции приёмки в `full-local.json`.
- [x] Добавить fail-closed preflight, source/image BOM и verifier неизменяемого evidence.
- [x] Добавить единый runbook для инженера без знания истории репозитория.
- [x] Реализовать единый исполняемый gate, который сам запускает одиннадцать реальных функций, формирует evidence и проверяет cleanup.
- [x] Создать отдельный provider-репозиторий и независимые подписываемые release workflows для provider/worker images.
- [x] Выпустить подписанный provider image на GitHub-hosted Linux x86_64 runner:
  `ghcr.io/sapogov1978/aelev-translation-provider@sha256:4a820a13e6a03188589c09d14e52b5f2d05e9359d7df847bad768966016e9ec9`.
- [ ] Выполнить workflow Translator worker и получить подписанный worker
  `name@sha256` из его digest-artifact.
- [ ] Предоставить одноразовое SPIRE approval и выполнить новый полный функциональный прогон на чистом стенде.
- [ ] После успешного прогона повторить Production Ready аудит точного release commit.

Владельческие незавершённые работы по-прежнему ведутся в `TASKS.md` соответствующего продукта и здесь не дублируются. Эти два пункта относятся к межвладельческой композиции и потому остаются в корневом списке.
