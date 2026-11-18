# Защищённый сетевой Kafka: компонентная проверка

Native arm64 прогон завершился `PASS` за **53.826 с**: [исходный JSON](network-profile-result-20261118.json), [точные source/image bindings](network-profile-bindings-20261118.json). SHA-256 результата — `2eef07c714bcfc12287ee4df6888a4ffed368cba9fbae7a6d7e81f6ca6a47755`. Полный publisher runtime, release admission, production issuer/renewal, физические разные компьютеры и Kafka HA этим результатом не подтверждаются.

## Что проверено до изменения

Полная история владельца Kafka в текущем и исходном репозиториях не содержит поставляемого TLS-рецепта: README/USER_MANUAL прямо ограничивали его локальным PLAINTEXT. Предыдущий native network test (`8ec6c3b`) уже использовал Kafka 3.9.1 PEM/mTLS и StandardAuthorizer; эта конфигурация переиспользована как основание, без нового брокера или библиотеки. C-проба уже принимала явный loopback port. Издатель оставался на hardcoded `kafka:9092`; его история не содержит настраиваемого TLS endpoint.

Сопоставлены broker startup/defaults, Kafka principal/DN/ACL, routing/advertised ports, native health, publisher TLS/UNKNOWN, storage/cluster ID, repeat/handoff, каталоги и владельческие inputs. Первый настоящий сетевой запуск показал отсутствие фактической публикации порта у `internal` network, хотя HostConfig содержал port binding. Сетевой overlay меняет только собственную сеть на routable; local recipe сохраняет изоляцию. Старый publisher image воспроизвёл plaintext regression, новый прошёл 12 native tests без source overlay.

## Решения, официальные основания и прогноз

| Решение | Основание | Прогноз до правки |
|---|---|---|
| Native PEM/mTLS, обязательный client certificate и hostname verification | [Kafka 3.9 SSL/PEM](https://kafka.apache.org/39/security/encryption-and-authentication-using-ssl/) | 45–60 минут на профиль и native verification |
| KRaft StandardAuthorizer; exact broker/operator DNs; default deny | [Kafka ACL](https://kafka.apache.org/39/security/authorization-and-acls/) | В составе того же блока |
| Непубликуемые controller/health loopback listeners; маршрутизируемый client listener | [Kafka listener configuration](https://kafka.apache.org/39/security/listener-configuration/), [Compose internal network](https://docs.docker.com/reference/compose-file/networks/#internal) | В составе того же блока |
| Прежний image entrypoint с именованным CLIENT:SSL и PEM, без специальных JKS passwords | [Configure Kafka image 3.9.1](https://github.com/apache/kafka/blob/3.9.1/docker/resources/common-scripts/configure#L62-L90) | В составе того же блока |
| Сохранение cluster ID и отказ до format чужого store | [Formatter 3.9.1](https://github.com/apache/kafka/blob/3.9.1/metadata/src/main/java/org/apache/kafka/metadata/storage/Formatter.java#L361-L377) | В составе restart/restore проверки |
| Явный endpoint и прежний SDK TLS helper издателя; ошибки остаются UNKNOWN | [Python 3.13 TLS client](https://docs.python.org/3.13/library/ssl.html#ssl.create_default_context) | Дополнительно 15–20 минут с native сборкой |

Правила exact source/image inputs, изоляции secrets, local compatibility и ownership cleanup — политика продукта, не предписание Apache/Docker. Принятые версии Kafka 3.9.1, Python 3.13.7, Alpine 3.22, SQLite 3.53.4 и SDK 1.1.2 сохраняются. Broker image и C binary не пересобирались, publisher image собран без tag/push; fingerprints привязаны к фактическим байтам внутри нового образа. Состав каталога — прежние 94 роли, schema 1; новый файл разрешён только соответствующим owner roles.

## Фактический scope

- Owner workspace подготовлен и broker запущен через действующие pinned owner procedures/node journal. Повтор start вернул прежний receipt без его переписи; fresh status подтвердил health.
- Реальный Kafka client из другой bridge network использовал опубликованный endpoint. Metadata сообщил именно этот advertised address. Общая с брокером Docker DNS-сеть клиенту не требовалась.
- Отдельный client certificate с Read/Write/Describe своего технического topic и Read ограниченного group prefix успешно записал/прочитал сообщения. Чужое доверенное удостоверение не смогло писать; runtime client не смог создать новый topic.
- Отсутствующий сертификат, чужой issuer и неправильный server hostname отвергнуты. ANONYMOUS через внутренний health listener не смог записать сообщение; отдельного ACL/superuser ему нет.
- Реальный пересобранный publisher image со своим отдельным certificate, без admin key/ACL, получил ONLINE по mTLS. Неверное имя и недоверенный client дали UNKNOWN. Только native probe, без Platform UI enrollment/runtime start.
- Restart сохранил два сообщения и ACL. Холодная копия совпала побайтно по всем файлам; Formatter отверг иной cluster ID на копии без изменения этих байтов. Новый broker на копии восстановил сообщения и запрет чужой записи; старый писатель оставался остановлен.
- Cleanup проверил ownership всей новой описи до удаления и восстановил исходные containers/volumes/networks/images. Все CA keys, CLI output и закрытые requests остаются вне Git в private evidence.

Вспомогательные ошибки проверочного сценария сохранены отдельно: несовпадение формы direct owner-prepare request с documented API и неполный config отрицательного Formatter case не объявлялись исправлениями Kafka. Два native Compose versions по-разному опускают явные false-поля в JSON; test assertions используют эквивалентное false-значение без изменения runtime. Новые tests не обходят image/source admission в штатном installer.

После экспорта закрытых evidence удалены четыре private fixture workspace внутри тестовой установки. Внутренний Docker остался без контейнеров и volumes, только с исходными bridge/host/none networks; свой Engine остановлен. Отдельное сравнение внешней описи подтвердило неизменность Image, Name, Running, Paused и StartedAt всех 159 остальных контейнеров и сохранность полного исходного набора контейнеров. Общий Docker не перезапускался, prune не выполнялся. Сводка cleanup сохранена в публичных bindings; закрытые credentials не включены в отчёт.

Проверки: **262** infrastructure tools, **11** Kafka configuration и **12** native publisher cases passed; Ruff/Pyright/Black и Bandit 1.8.6 проходят без расширения baseline. Продолжение — доставка реальных владельческих credentials/ACL/topics и запуск consumer workflow; положительный технический topic не выдаётся за предметный сквозной проход или качество модели. Канонические очереди, keys и данные других установок не использовались; RESUME/OpenAI отсутствуют.
