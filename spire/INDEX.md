# SPIRE: карта каталога

Эта агентская карта дополняет Graphify высокоуровневым ответом на вопрос «что и где искать» в каталоге владельца.

| Что искать | Где находится |
|---|---|
| Назначение и состав | [README.md](README.md) |
| Владение, аттестация, обновление, отзыв и восстановление | [SPEC.md](SPEC.md) |
| Установка, команды и диагностика | [USER_MANUAL.md](USER_MANUAL.md) |
| Полный bootstrap из закрытого initial bundle | `bootstrap.py`, `src/aelev_spire/observation-inputs.json`; [публичный вход](README.md#запуск) |
| Локальная подготовка PKI и трёх запросов сопровождения | `local-inputs.py`; [формат входов](USER_MANUAL.md#подготовить-локальный-initial-bundle) |
| Три отдельные операторские привязки и настоящая доставка сертификатов | `../tools/spire_renewal.py`, `test/test_real_renewal.py`; [сопровождение](USER_MANUAL.md#автоматическое-сопровождение-локальных-сертификатов) |
| Технические входы | [ENDPOINTS.md](ENDPOINTS.md) |
| Самостоятельные роли и сетевой источник публичного состояния | `src/aelev_spire/components.py`, `source_setup.py`, `public_state.py`, `network.py`; [руководство](USER_MANUAL.md#самостоятельные-роли-наблюдения) |
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
