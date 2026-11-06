# Проверка выбираемого ограждения PostgreSQL

Дата: 2026-10-10. Итог локального Docker-режима: PASS. Это не допуск Kubernetes или аппаратного watchdog.

| Проверка | Результат |
|---|---|
| Закреплённый стек | PostgreSQL 17.6, Patroni 4.1.5, etcd 3.6.10, Python 3.13.7, прежние Alpine base images/digests; ОС и Python не менялись. |
| Сборка | Собственный runtime и `checks` собраны; итоговое изменение описания UI применено только собственному издателю. |
| PostgreSQL: модульные и конфигурационные проверки | 61 PASS на Python 3.13.7: три механизма, нативный hook, отказы DCS/runtime, владельцы контейнеров, Pod UID, terminal status, состав UI и ограниченный Kubernetes-манифест. |
| PostgreSQL: качество кода | Black и Ruff PASS; строгий MyPy PASS для 8 исходных файлов, включая генератор и два операторских теста. |
| Оркестрация: регрессии | 30 PASS на Python 3.13.7; Black/Ruff PASS; строгий MyPy PASS для 4 исходных файлов. Закреплённый Playwright 1.56.0 использован только как установленный сторонний источник типов, браузер не исполнялся. |
| Сервисная/общая модель | Полное сравнение всех ролей PostgreSQL и четырёх прежних владельцев PASS, включая параметры, зависимости, mounts, проверки и секреты; раскрытые значения не выводились. |
| Реальный Docker через корневой Compose | PASS: [fencing-orchestration-result.md](fencing-orchestration-result.md). Запущены только собственные PostgreSQL-компоненты; весь комплекс повторно не перезапускался. |
| Реальный Docker через сервисный Compose | PASS: [fencing-lifecycle-result.md](fencing-lifecycle-result.md). Заморожен настоящий дочерний HA-процесс Patroni, не только его PID 1 init; SQL старого primary оставался жив. |
| Квитанции и данные | Завершение обоих peer-процессов записано до подтверждения; изменился момент старта прежнего контейнера, один новый primary, хеш 1000 синхронно подтверждённых строк совпал. |
| Отказ исполнителя | Без подтверждения завершения реплики не продвинулись после периода больше TTL; после возврата связи один primary восстановлен и новая синхронная запись подтверждена. |
| Сохранность и очистка | Удалены только собственные временные таблицы и семь контейнеров/пустая сеть корневой проверки. Прежние SQL-данные, семь прежних томов, TLS и состояния других проектов сохранены. Новый том журнала ограждения оставлен как рабочие данные. |
| Собственное состояние | Все три PostgreSQL, три etcd и исполнитель ONLINE; восемь контейнеров с издателем healthy. Реальный mTLS probe может читать health, POST `/fence` отвергнут с 403. |
| Документация | Относительные ссылки, пары code fences, заголовок Mermaid, отсутствие конфликтных маркеров и хвостовых пробелов проверены; CHANGELOG, спецификации и реестр ADR обновлены. |

## Команды PostgreSQL

Из каталога поставки модульные проверки не получают Docker socket:

```sh
docker build --target checks -t aelev/postgresql-checks:local .
docker run --rm -e PYTHONPATH=/work/src:/work -v "$PWD:/work" \
  aelev/postgresql-checks:local sh -c \
  'black --check src prepare-tls.py status_publisher.py kubernetes test && ruff check src prepare-tls.py status_publisher.py kubernetes test && mypy src prepare-tls.py status_publisher.py kubernetes test/fencing-lifecycle.py test/cluster-lifecycle.py && pytest -q test/test_runtime.py test/test_configuration.py test/test_fencing.py test/test_status_publisher.py test/test_kubernetes_fencing.py'
python3.13 test/fencing-lifecycle.py
```

Для корневой модели оператор передаёт `--compose-file ../compose.yml`; тест сохраняет отдельный отчёт. При отсутствии host Python 3.13 собственный доверенный runner с операторскими runtime-полномочиями описан в [FENCING.md](../FENCING.md). Испытания создавали только точно названную временную таблицу в реальном существующем кластере и удалили её; отдельного фиктивного PostgreSQL/DCS не было.

## Что не проверено

Реальное завершение Pod/kubelet, сетевая изоляция Kubernetes Node, CSI и серверный dry-run не выполнялись: `kubectl config current-context` сообщил, что context не настроен. Проверены код отказов и генерируемые полномочия, не работа реального Kubernetes. Генератор поставляет только исполнитель; миграция самого PostgreSQL в StatefulSets не выполнялась.

Аппаратный watchdog не открывался и не взводился: станция общая, Docker Desktop, проверка могла бы перезагрузить чужие workloads. Разрешение Compose и требуемый режим Patroni проверены, аппаратное учение требует выделенной среды. Полный старый `cluster-lifecycle.py` со всеми сценариями backup/PITR повторно не запускался из-за существующих данных; исторический отчёт не переписан. Географический WAN, независимые области отказа, внешний backup и supply-chain допуск остаются отдельными воротами [TASKS.md](../TASKS.md).

## Текущее состояние

В игнорируемом сервисном `.env` установлен `AELEV_INFRASTRUCTURE_POSTGRESQL_FENCING_MODE=docker`. Самостоятельный Compose запущен с профилем `ui`; `watchdog`-устройств и перезагрузки host в этом механизме нет. Новый журнал ограждения и существующие данные сохранены; `.env` и TLS не отслеживаются Git. Коммит и push в этой задаче не выполнялись.
