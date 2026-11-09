# Руководство: Секреты translator-service

Каталог монтируется только для чтения в PostgreSQL и роли translator-service.
Отдельный игнорируемый `.env.local` provider монтируется только в непрозрачный
provider image как `/run/secrets/provider/credential.env`; Translator его не
читает. Файлы,
кроме документации, игнорируются Git и не должны попадать в журналы, команды с
подстановкой содержимого или артефакты сборки.

Для локального запуска создайте пароль собственной БД и ограничьте права файла:

```sh
umask 077
mkdir -p secrets
test ! -e secrets/postgres-password || exit 1
openssl rand -out secrets/postgres-password -hex 32
chmod 600 secrets/postgres-password
```

API key генерируется и отзывается у выбранного поставщика провайдера. В
игнорируемом `TranslationProvider/.env.local` вручную заменяется только
плейсхолдер `OPENAI_API_KEY`; файл должен иметь права `0600`. Инфраструктура
проверяет mount и не знает model ID, endpoint или prompt.

При `PLAINTEXT` и `sslmode=disable` транспортные TLS-файлы не требуются, а соответствующие переменные имеют точное значение `NONE`; это не отменяет материал API/OIDC и издателя выбранного режима. Для production доставьте CA, клиентский сертификат и ключ Kafka и PostgreSQL, задайте в `.env` только имена без пути и установите права, доступные контейнерному UID `10001` только на чтение. Текущий общий bind этого каталога требует не размещать внутри него пароли администратора UI, серверный API-ключ и закрытые CA. Полный перечень, происхождение файлов, отдельный UI_ADMIN_ROOT, ограничения deployment key и ротация описаны в [README](../USER_MANUAL.md#установка-и-запуск). Приватные ключи и пароль не коммитятся.

## Официальные руководства

- [Kafka SSL](https://kafka.apache.org/39/documentation/#security_ssl) и [ACL authorization](https://kafka.apache.org/39/documentation/#security_authz) — разделение runtime/deployment identities.
- [PostgreSQL client certificates](https://www.postgresql.org/docs/17/auth-cert.html) и [libpq SSL](https://www.postgresql.org/docs/17/libpq-ssl.html) — `verify-full`, CA и client key.
- [Docker Compose secrets](https://docs.docker.com/reference/compose-file/secrets/) — файловая доставка минимальному набору ролей.
- [OpenSSL verify](https://docs.openssl.org/3.0/man1/openssl-verify/) — проверка цепочки, purpose и SAN до публикации.
