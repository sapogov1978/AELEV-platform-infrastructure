# Keycloak: карта каталога

Эта агентская карта дополняет Graphify высокоуровневым ответом на вопрос «что и где искать» в каталоге владельца.

| Что искать | Где находится |
|---|---|
| Назначение, состав и запуск | [README.md](README.md) |
| Подготовка, эксплуатация и восстановление | [USER_MANUAL.md](USER_MANUAL.md) |
| Создание, восстановление и проверка полной резервной копии | [recovery.py](recovery.py) |
| Перенос прежних env-паролей без ротации | [migrate_password_files.py](migrate_password_files.py), процедура в [руководстве](USER_MANUAL.md#перенос-прежних-паролей-из-env-без-ротации) |
| Фактический результат восстановления | [test/recovery-result.md](test/recovery-result.md) |
| Совместимость recovery с текущим TLS directory mount и Linux runtime GID | [План](test/recovery-directory-plan-20261119.md), [результат](test/recovery-directory-result-20261119.md), [bindings](test/recovery-directory-bindings-20261119.json) |
| Нормативные гарантии | [SPEC.md](SPEC.md) |
| Происхождение секретов | [secrets/README.md](secrets/README.md) |
| Текущие задачи и допуск площадки | [TASKS.md](TASKS.md) |
| История изменений | [CHANGELOG.md](CHANGELOG.md) |
| Настройки и запуск | `.env.example`, `compose.yml`, `compose.local-bridge.yml`, `realm.json` |
| Раздельные SQL/Keycloak placements, mTLS/JDBC и management HTTPS | `compose.network.yml`, `network.env.example`, `database-identity-prepare.sh`, `database-pg_hba.conf`; [руководство](USER_MANUAL.md#независимое-размещение-ролей), [план](test/network-profile-plan-20261119.md), [результат/bindings](test/network-profile-result-20261119.md) |
| Наследуемая тема входа | `themes/aelev/login/`; поведение — [SPEC.md](SPEC.md#восстановление-формы-входа) |
| Проверки исправления формы и результат выкладки | [test/login-recovery-result.md](test/login-recovery-result.md) |
| Проверки и результаты | `test/` |

Общая человеческая identity и разделение клиентских ролей определены [00-INFRA-20261106-0007-workload-and-operator-identity](../docs/adr/00-INFRA-20261106-0007-workload-and-operator-identity.md).
