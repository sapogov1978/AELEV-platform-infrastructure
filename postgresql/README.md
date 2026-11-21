# PostgreSQL

Транзакционное хранилище PostgreSQL 17 с Patroni и etcd.

## Основные возможности

- Три SQL-узла и синхронная репликация.
- Выбор ведущего и ограждение.
- TLS, резервирование и восстановление.

## Запуск

Для явной подсети собственной сети отдельного проекта можно выбрать [IPAM overlay](../docs/api/postgresql-ipam.md) после runtime recipe. Он требует только два явных IPAM inputs, сохраняет прежние модели без overlay и не относится к отдельному SSH fencer. CIDR/gateway, отсутствие пересечений и соответствие действительных etcd peer SAN проверяет deployment planner.

Первоначальную установку bounded SSH node operation предоставляет [публичный node-install API](../docs/api/postgresql-node-installation.md): source-pinned launcher, собственные labelled volumes и настоящий `initialize`. OS/SSH/admin policy узла подготавливает платформа; host tests не подменяют native installation receipt.

После подготовки по [руководству](USER_MANUAL.md), из этого каталога:

```sh
docker compose config --quiet
docker compose up -d --no-build --pull never
```

Первоначальная сборка, обновление и восстановление описаны в руководстве. При работе через общую инфраструктурную композицию не запускайте отдельный проект поверх тех же данных.

Локальное продление SQL leaf поддерживает OpenSSL 3.0 через CSR прежнего ключа; публикация по-прежнему требует неизменного scope и отдельной успешной проверки SQL-кандидата.

## Навигация

- [architecture.mmd](architecture.mmd) — диаграмма границы, компонентов и основных потоков владельца.
- [USER_MANUAL.md](USER_MANUAL.md), [SPEC.md](SPEC.md) — использование и правила продукта.
- [FENCING.md](FENCING.md) — механизм ограждения.
- [FENCING_SSH.md](FENCING_SSH.md) — кандидат независимого размещения Docker fencer с общим узловым бюджетом его копий.
- [INDEX.md](INDEX.md), [TASKS.md](TASKS.md), [CHANGELOG.md](CHANGELOG.md) — агентская карта, ограничения и история.
