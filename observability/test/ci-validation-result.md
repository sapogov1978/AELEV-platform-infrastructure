# Observability — исправление и локальное воспроизведение CI

Дата: 2026-10-11. Итог локальных проверок: PASS. Workflow `.github/workflows/observability-checks.yml` приведён к структуре ADR-0107, без восстановления общего runtime или отключения проверок. Прежний корневой `initialize.py` больше не вызывается; `delivery.py` и его регрессии проверяются только внутри delivery.

## Результаты

| Задача | Сборка checks | Ruff | Black | MyPy | Pytest |
|---|---|---|---|---|---|
| Слой оркестрации | PASS | PASS | PASS | PASS | 3 passed |
| delivery | PASS | PASS | PASS | PASS | 50 passed |
| otel-collector | PASS | PASS | PASS | PASS | 16 passed |
| prometheus | PASS | PASS | PASS | PASS | 16 passed |
| blackbox-exporter | PASS | PASS | PASS | PASS | 16 passed |
| loki | PASS | PASS | PASS | PASS | 16 passed |
| tempo | PASS | PASS | PASS | PASS | 16 passed |
| grafana | PASS | PASS | PASS | PASS | 16 passed |

Всего 149 тестов, без пропусков. Все восемь образов собраны заново из текущих собственных контекстов и тестовых контекстов; запускались точные команды workflow, без подмены исходников bind mount. Слой использует собственный `Dockerfile` и проверяет `prepare.py`, `test` и `test/test_orchestration.py`. Каждый продукт использует свой `Dockerfile.status`, `.env.example`, Poetry lock и `test`; команды: `ruff check .`, `black --check .`, `mypy .`, `pytest -p no:cacheprovider`.

База продуктов прочитана из собственного `UI_PYTHON_IMAGE`: прежний Python 3.13.7 / Alpine 3.22 с прежним SHA-256. Слой сохраняет ту же закреплённую базу. Версии ОС/Python, зависимостей и `actions/checkout`, `ubuntu-24.04`, триггеры `push`/`pull_request`/`workflow_dispatch` и права `contents: read` не изменены. Матрица ровно соответствует семи каноническим include; `fail-fast: false` не отменяет остальные продукты при ошибке одного.

Синтаксис workflow и встроенные shell-команды проверены официальным `actionlint` 1.7.12: PASS. Отдельно проверены соответствие состава матрицы, запрет сохранения checkout credentials, триггеры, runner и минимальные права. Удалена только одна лишняя пустая строка в каждом из семи `test/status_publication.py`; равенство Python AST до/после подтверждено, поведение не менялось.

## Изоляция и ограничения

Все проверки выполнялись в `docker run --rm --network none` без Docker socket, рабочих env и секретов. `docker compose config` внутри тестов разрешает модель, но не запускает сервисы. TLS/HTTP-регрессии delivery используют его настоящий код и локальные временные удостоверения/процессы внутри одноразового контейнера; это не испытание внешней рабочей PKI.

После проверок исходные 71 контейнер и их идентификаторы сохранены, новых оставшихся контейнеров нет. Рабочие процессы, тома, сети и данные не останавливались и не удалялись. Образы checks являются результатами сборки; тестовые временные файлы исчезли вместе с одноразовыми контейнерами.

Это локальное воспроизведение команд, не новый успешный прогон GitHub Actions. Workflow ещё не отправлен на GitHub в рамках этого исправления; исторический упавший запуск не меняется задним числом. Локальная Docker Desktop-проверка не подтверждает работу GitHub runner, полный жизненный цикл, WAN, нагрузку, production PKI или резервное восстановление. Прежние несвязанные изменения UI/переводчика в рабочем дереве сохранены.
