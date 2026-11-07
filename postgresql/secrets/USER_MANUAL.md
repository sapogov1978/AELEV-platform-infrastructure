# Руководство: Секреты PostgreSQL

Полный порядок получения, первоначальной генерации, хранения и ротации, включая собственную identity издателя, описан в [README владельца](../USER_MANUAL.md#секреты-получение-и-подготовка). Этот файл перечисляет формат доставки, не заменяет эксплуатационную инструкцию.

Содержимое кроме этого README игнорируется Git и исключено из образа. Каталоги создаёт явный операторский `prepare-tls.py`; существующий материал не перезаписывается. Формат сертификатов/ключей — PEM, паролей — непустой UTF-8 файл. Runtime копирует только материал своей роли в tmpfs с правами `0600`.

- `node-N`: `server.crt/key`, `ca.crt`, `admin.crt/key`, `admin-password`, `replication.crt/key`, `replication-password`, `etcd-ca.crt`, `etcd-client.crt/key`.
- `etcd-N`: `server.crt/key`, `ca.crt`, `client.crt/key` для собственной проверки здоровья. Серверный сертификат имеет serverAuth и clientAuth для peer-связи.
- `admin`: `ca.crt`, `admin.crt/key`, `admin-password`, `provision-password`; локальный генератор также хранит здесь исходную репликационную identity, которую не следует доставлять обычному операторскому заданию без необходимости.
- `issuer`: отдельные `postgresql.crt/key` и `etcd.crt/key`; приватные CA-ключи остаются у оператора и никогда не монтируются в контейнеры.
- `fencing` (только Docker/Kubernetes): `server.crt/key` с SAN адресов исполнителя и `serverAuth`, `ca.crt`, `admin.crt/key`, `etcd-ca.crt`, `etcd-client.crt/key`. Пароли SQL, секреты UI и приватные ключи CA исполнителю не нужны. Административный CN разрешает `/fence`, CN `postgresql-probe` — только GET `/health`. Первоначальный генератор создаёт каталог; `prepare-tls.py --fencing-only` добавляет только его к существующей локальной PKI и отказывает при повторе. Перед запуском каталогу задают `0750`, файлам `0640` и числовую группу из `FENCING_SECRET_GID`; исполнитель получает эту группу только для чтения, а world-readable ключи запрещены. В Kubernetes тот же набор PEM поступает из утверждённого внешнего источника через CSI.

CN SQL-клиента должен точно совпадать с ролью. Административные и репликационные права не выдаются рабочему приложению. Серверные SAN соответствуют адресу своего узла из .env. Для ротации используйте утверждённую PKI, смену материала по одному узлу и повторную проверку здоровья/репликации; истёкшее удостоверение не допускает незашифрованный fallback.

## Официальные руководства

- [PostgreSQL SSL support](https://www.postgresql.org/docs/17/ssl-tcp.html) и [client certificate authentication](https://www.postgresql.org/docs/17/auth-cert.html) — CA, CRL, client certificate и role mapping.
- [Patroni TLS configuration](https://patroni.readthedocs.io/en/latest/yaml_configuration.html#rest-api) — защита REST API и сертификаты.
- [etcd transport security](https://etcd.io/docs/v3.6/op-guide/security/) — peer/client TLS и разделение доверия.
- [Docker Compose secrets](https://docs.docker.com/reference/compose-file/secrets/) — выдача файлов только явно перечисленным ролям.
