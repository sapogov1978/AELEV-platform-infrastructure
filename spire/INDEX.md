# SPIRE: карта каталога

Эта агентская карта дополняет Graphify высокоуровневым ответом на вопрос «что и где искать» в каталоге владельца.

| Что искать | Где находится |
|---|---|
| Назначение и состав | [README.md](README.md) |
| Владение, аттестация, обновление, отзыв и восстановление | [SPEC.md](SPEC.md) |
| Установка, команды и диагностика | [USER_MANUAL.md](USER_MANUAL.md) |
| Технические входы | [ENDPOINTS.md](ENDPOINTS.md) |
| Текущее состояние и непроверенные ворота | [TASKS.md](TASKS.md) |
| История изменений | [CHANGELOG.md](CHANGELOG.md) |
| Проверка рабочего стенда 20 октября | [test/stand-20261020.md](test/stand-20261020.md) |
| Канонические закрепления и самостоятельная сборка | `artifacts.lock.json`, `pyproject.toml`, `poetry.lock`, `Dockerfile` |
| Модели сервера/оператора, агента и наблюдения | `compose.yml`, `compose.agent.yml`, `compose.observation.yml`, `.env.example` |
| Публичная привязка установленного провайдера к общей композиции | `compose.group.yml`, `compose.default.yml`, `export.sh`, `export.ps1`, `src/aelev_spire/composition.py`; [проверка 23 октября](test/composition-export-20261023.md) |
| Зашифрованное резервирование и восстановление остановленных писателей | `src/aelev_spire/backup.py` |
| Отдельное восстановление sender без пополнения бюджета | `src/aelev_spire/sender_recovery.py`, [результат](test/sender-recovery-20261020.md) |
| Закреплённый публичный клиент Platform UI | `vendor`, `pyproject.toml`, `poetry.lock` |
| Реализация и проверки без каталога потребителей | `src/aelev_spire`, `test` |
