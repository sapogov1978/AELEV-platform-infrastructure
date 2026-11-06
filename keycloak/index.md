# Keycloak: карта каталога

| Что искать | Где находится |
|---|---|
| Назначение, состав и запуск | [README.md](README.md) |
| Подготовка, эксплуатация и восстановление | [USER_MANUAL.md](USER_MANUAL.md) |
| Создание, восстановление и проверка полной резервной копии | [recovery.py](recovery.py) |
| Перенос прежних env-паролей без ротации | [migrate_password_files.py](migrate_password_files.py), процедура в [руководстве](USER_MANUAL.md#перенос-прежних-паролей-из-env-без-ротации) |
| Фактический результат восстановления | [test/recovery-result.md](test/recovery-result.md) |
| Нормативные гарантии | [SPEC.md](SPEC.md) |
| Происхождение секретов | [secrets/README.md](secrets/README.md) |
| Текущие задачи и допуск площадки | [TASKS.md](TASKS.md) |
| История изменений | [CHANGELOG.md](CHANGELOG.md) |
| Настройки и запуск | `.env.example`, `compose.yml`, `realm.json` |
| Наследуемая тема входа | `themes/aelev/login/`; поведение — [SPEC.md](SPEC.md#восстановление-формы-входа) |
| Проверки исправления формы и результат выкладки | [test/login-recovery-result.md](test/login-recovery-result.md) |
| Проверки и результаты | `test/` |

Общая человеческая identity и разделение клиентских ролей определены [ADR-0119](../docs/adr/20261013-0119-unified-platform-operator-sso.md).
