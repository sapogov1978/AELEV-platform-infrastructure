# SPIRE: карта каталога

- [README](README.md) — назначение и состав.
- [SPEC](SPEC.md) — владение, аттестация, обновление, отзыв и восстановление.
- [USER_MANUAL](USER_MANUAL.md) — установка, команды и диагностика.
- [ENDPOINTS](ENDPOINTS.md) — технические входы.
- [TASKS](TASKS.md) — текущее состояние и непроверенные ворота.
- [CHANGELOG](CHANGELOG.md) — история.
- [Проверка рабочего стенда 20 октября](test/stand-20261020.md) — установка, Platform UI, Grafana, причины отказов и границы локального допуска.
- `artifacts.lock.json`, `pyproject.toml`, `poetry.lock`, `Dockerfile` — канонические закрепления и самостоятельная сборка.
- `compose.yml`, `compose.agent.yml`, `compose.observation.yml`, `.env.example` — собственные модели сервера/оператора, агента и наблюдения.
- `compose.group.yml`, `compose.default.yml`, `export.sh`, `export.ps1`, `src/aelev_spire/composition.py` — публичная привязка уже установленного провайдера к общей композиции; [проверка 23 октября](test/composition-export-20261023.md).
- `src/aelev_spire/backup.py` — зашифрованное резервирование и восстановление только собственных остановленных писателей.
- `src/aelev_spire/sender_recovery.py`, `test/sender-recovery-20261020.md` — отдельное восстановление sender без пополнения бюджета; фактическая установка лёгкой пробы и сохранность журнала.
- `vendor` — неизменяемый публичный клиент Platform UI 1.1.1; версия и SHA-256 закреплены Poetry lock, исходники соседних продуктов не нужны сборке.
- `src/aelev_spire` и `test` — реализация и проверки, без каталога потребителей.
