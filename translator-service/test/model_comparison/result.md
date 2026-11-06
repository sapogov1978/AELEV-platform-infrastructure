# Сравнение MADLAD и Index-Translate

Время: 2026-10-11T14:44:20.240472+00:00

Операторский эксперимент, не смена рабочей модели. CPU-only, 2 CPU на вычислитель, одна модель эксперимента одновременно; рабочая модель не заменяется. Разрешённые временные остановки вычислителя и восстановление фиксируются отдельно в maintenance-result.json.

| Модель | Переводы | p50, с | p95, с | Пиковая cgroup-память, ГиБ | Нарушения точных условий |
|---|---:|---:|---:|---:|---:|
| madlad | 40 | 8.01 | 17.67 | 2.92 | 2 |
| index-2b | 40 | 7.28 | 11.25 | 1.42 | 0 |
| index-9b | 40 | 15.10 | 25.29 | 5.71 | 0 |

9B измерялся с разрешённой остановкой рабочего model/worker; результаты MADLAD/2B сохранены из предыдущего запуска без этой остановки. Лимит CPU одинаков, но фоновая нагрузка не идентична.


Память cgroup включает процесс и файловый кэш, не только веса. p95 — ближайший верхний ранг небольшой выборки, не нагрузочный SLO. Точные условия проверяют заданные идентификаторы, числа и добавление страны, а не правильность всего смысла. Q4_K и Q4_K_M, архитектуры и токенизаторы различаются.

## Смысловая оценка и вывод

Исполнение всех трёх моделей завершено: 120 настоящих переводов, 20 текстов × 2 вычисления каждой моделью. У всех 20 пар каждой модели одинаковые байты ответов; у Index нет обрезки по выходному пределу, а `cached_tokens=0` подтверждён в ответах 9B. Отсутствие нарушения литералов не означает правильности смысла. Это ручной разбор короткого синтетического корпуса, не независимая экспертная аттестация или оценка всей языковой области модели.

| Пример | MADLAD | Index 2B | Index 9B |
|---|---|---|---|
| `climate observatory` | Добавил `von Deutschland`, хотя страна не указана | `Klimaobservatorium`, без добавки | `Klimaobservatorium`, без добавки |
| «Замок на двери ... замок на холме» | Верно: `lock` / `castle` | Ошибка: `castle door`, сломанной стала дверь, а не замок | Верно: `lock` / `castle` |
| Предметная «сверка» | Точный термин `reconciliation` | Более общее `verification` | Точный термин `reconciliation` |
| `received 17 crates and rejected 3` | Получение 17 и отклонение 3 без уточнения связи | То же, без уточнения связи | Добавил «3 из которых»: оригинал не устанавливает, что отклонённые входят в 17 |
| Запрос `is pending` | «Отложен»: неоправданный оттенок отсрочки | «Находится в ожидании» | «Находится в обработке»: неоправданное уточнение состояния |

Найденные особенности сохраняются в обоих повторах. Числа и placeholders в последних двух строках формально правильны, но смысл может измениться: больший размер модели не устраняет такой риск. В остальных рассмотренных коротких примерах не обнаружена сопоставимая потеря основных отрицаний, идентификаторов или условий; это не доказательство отсутствия других ошибок.

2B — самый экономный кандидат этого запуска: 1,42 ГиБ cgroup и p50 7,28 с, но он не может считаться смыслово эквивалентной заменой. 9B исправил неоднозначность и предметный термин, однако потребовал примерно в четыре раза больше cgroup-памяти и показал примерно вдвое большую медиану относительно 2B; эти отношения описывают только данный стенд с разной фоновой нагрузкой. Безусловного победителя по смыслу на этих 20 примерах нет. Рабочий MADLAD оставлен без изменения; для решения о замене требуется более широкий независимый корпус нужных языков и проверка длинных текстов, терминологии, эксплуатационной совместимости и восстановления.

VmHWM нативного процесса Index 2B — 1,36 ГиБ, Index 9B — 5,53 ГиБ. Для MADLAD этот показатель в первом этапе не собирался; его отсутствие нельзя записывать как нулевую память. Cgroup, RSS и размер GGUF — разные измерения.

## Восстановление рабочей поставки

Для 9B штатно остановлены только собственные `translator-worker` и `translator-model`; до запуска вычислителя доступная память VM составила 8,82 ГиБ при бюджете модели 6,5 ГиБ и запасе 0,5 ГиБ. Остальные компоненты не выключались. После эксперимента восстановлены те же ID, образы и тома, модель healthy, worker и остальные собственные роли ONLINE, `/readyz` успешен. [Опись и результат восстановления](maintenance-result.json) сохраняют разрешённые изменения времени старта.

Опись Docker на границах текущего вычислительного этапа совпала. Предыдущий этап имел отдельное нарушение описи: один внешний исходный контейнер перезапустился без команды управления от эксперимента, сохранив ID/образ; причина не установлена. Этот факт не удалён и остаётся в `previous_run` файла [result.json](result.json). Успех текущего этапа не переписывает предыдущую проверку.

Очистка завершена: собственные временные веса, промежуточные файлы и два экспериментальных образа удалены; контейнеров эксперимента нет. Рабочие контейнеры, секреты, тома, данные, модельный и исходный тестовый образы сохранены. [Доказательство очистки](cleanup-result.json); [проверки и ограничения](validation-result.md). Тест, точные результаты и журналы остаются здесь; веса доступны для повторной загрузки по манифесту.

## Исходные тексты и полученные переводы

### climate-observatory-term (en → de)

```text
climate observatory
```

Что проверять: Центральный банк без добавления страны.

madlad (0.69 с):

```text
Klimaobservatorium von Deutschland
```

index-2b (3.42 с):

```text
Klimaobservatorium
```

index-9b (7.87 с):

```text
Klimaobservatorium
```

### climate-observatory-sentence (en → de)

```text
The climate observatory did not revise temperature estimates.
```

Что проверять: Банк не повысил ставки; страна не указана.

madlad (3.86 с):

```text
Die Klimaobservatorium hat die Zinsen nicht erhöht.
```

index-2b (4.40 с):

```text
Die Klimaobservatorium hat die Zinsen nicht erhöht.
```

index-9b (10.44 с):

```text
Die Klimaobservatorium hat die Zinssätze nicht erhöht.
```

### en-ru-negation (en → ru)

```text
The service must not delete the original document after translation.
```

Что проверять: Запрет удаления оригинала после перевода.

madlad (2.85 с):

```text
Служба не должна удалять оригинальный документ после перевода.
```

index-2b (4.97 с):

```text
Сервис не должен удалять оригинальный документ после перевода.
```

index-9b (11.51 с):

```text
Сервис не должен удалять исходный документ после перевода.
```

### ru-en-negation (ru → en)

```text
Платёж не подтверждён. Повторять перевод денег нельзя, пока не завершена сверка.
```

Что проверять: Неизвестный платёж запрещает повтор денежного перевода до сверки.

madlad (8.39 с):

```text
The payment is not confirmed. You cannot re-transfer the money until the reconciliation is completed.
```

index-2b (7.28 с):

```text
Payment has not been confirmed. You cannot repeat the money transfer until the verification is complete.
```

index-9b (14.78 с):

```text
Payment not confirmed. You cannot repeat the money transfer until the reconciliation is complete.
```

### en-ru-numbers (en → ru)

```text
On 2026-10-11, the warehouse received 17 crates and rejected 3. The total weight was 1250 kg.
```

Что проверять: Дата, 17 принятых, 3 отклонённых ящика, 1250 кг.

madlad (16.63 с):

```text
В 2026-10-11 на склад поступило 17 ящиков и отклонено 3. Общий вес составил 1250 кг.
```

index-2b (10.14 с):

```text
11 октября 2026 года склад принял 17 ящиков и отклонил 3. Общий вес составил 1250 кг.
```

index-9b (25.29 с):

```text
11 октября 2026 года на склад поступило 17 ящиков, 3 из которых были отклонены. Общий вес составил 1250 кг.
```

### ru-en-numbers (ru → en)

```text
Температура снизилась с 12 до 7 градусов за 24 часа. Образцы A-17 и B-03 не повреждены.
```

Что проверять: Температура снизилась, оба образца целы; сохранить числа и идентификаторы.

madlad (15.57 с):

```text
The temperature decreased from 12 to 7 degrees in 24 hours. Specimens A-17 and B-03 are undamaged.
```

index-2b (12.64 с):

```text
The temperature dropped from 12 to 7 degrees in 24 hours. Samples A-17 and B-03 are undamaged.
```

index-9b (21.05 с):

```text
The temperature dropped from 12 to 7 degrees over 24 hours. Samples A-17 and B-03 are undamaged.
```

### en-ru-uncertainty (en → ru)

```text
The report suggests that demand may decline, but the available evidence does not establish a cause.
```

Что проверять: Предположение, не достоверный прогноз; причина не установлена.

madlad (10.00 с):

```text
В докладе высказывается предположение о том, что спрос может сократиться, однако имеющиеся данные не позволяют установить причину этого.
```

index-2b (7.44 с):

```text
В отчете предполагается, что спрос может снизиться, но имеющиеся данные не позволяют установить причинную связь.
```

index-9b (14.21 с):

```text
В докладе предполагается, что спрос может снизиться, однако имеющиеся данные не позволяют установить причину этого.
```

### ru-en-ambiguity (ru → en)

```text
Замок на двери сломан, но старый замок на холме открыт для посетителей.
```

Что проверять: Первый замок — lock, второй — castle.

madlad (6.80 с):

```text
The lock on the door is broken, but the old castle on the hill is open to visitors.
```

index-2b (7.25 с):

```text
The castle door is broken, but the old castle on the hill is open to visitors.
```

index-9b (16.18 с):

```text
The lock on the door is broken, but the old castle on the hill is open to visitors.
```

### de-en-negation (de → en)

```text
Die Lieferung wurde nicht storniert. Sie wird erst nach der Sicherheitsprüfung freigegeben.
```

Что проверять: Доставка НЕ отменена; разрешена только после проверки.

madlad (5.28 с):

```text
The shipment has not been cancelled and will be released after the security check.
```

index-2b (5.90 с):

```text
The delivery has not been canceled. It will only be released after the security check.
```

index-9b (36.04 с):

```text
The delivery was not canceled. It will only be released after the security check.
```

### en-de-technical (en → de)

```text
Restart the worker only after the database is ready. Keep the request_id unchanged.
```

Что проверять: Сначала готовность БД, потом перезапуск; неизменный идентификатор.

madlad (9.40 с):

```text
Starten Sie den Worker erst neu, nachdem die Datenbank bereit ist. Lassen Sie die request_id unverändert.
```

index-2b (6.98 с):

```text
Starte den Worker erst wieder, nachdem die Datenbank bereit ist. Halte die request_id unverändert.
```

index-9b (22.90 с):

```text
Starte den Worker erst wieder, nachdem die Datenbank bereit ist. Lass die request_id unverändert.
```

### fr-en (fr → en)

```text
Le contrat est valable jusqu'au 31 décembre, sauf si les deux parties décident de le résilier plus tôt.
```

Что проверять: Действует до 31 декабря, кроме согласованного досрочного расторжения.

madlad (9.87 с):

```text
The contract is valid until December 31, unless both parties decide to terminate it earlier.
```

index-2b (7.25 с):

```text
The contract is valid until December 31, unless both parties decide to terminate it earlier.
```

index-9b (14.45 с):

```text
The contract is valid until December 31, unless both parties decide to terminate it earlier.
```

### es-ru (es → ru)

```text
Aunque el servidor está disponible, los datos aún no se han actualizado. No confunda disponibilidad con actualidad.
```

Что проверять: Доступность сервера не подтверждает свежесть данных.

madlad (8.24 с):

```text
Хотя сервер доступен, данные еще не обновлены, не путать доступность с актуальностью.
```

index-2b (7.58 с):

```text
Хотя сервер доступен, данные еще не обновлены. Не путайте доступность с актуальностью.
```

index-9b (14.08 с):

```text
Хотя сервер доступен, данные еще не были обновлены. Не путайте доступность с актуальностью.
```

### zh-en (zh → en)

```text
系统已保存原始文件，但尚未完成翻译。网络恢复后将继续处理，不会删除历史记录。
```

Что проверять: Оригинал сохранён, перевод не завершён, продолжение после восстановления сети без удаления истории.

madlad (13.36 с):

```text
The original documents have been saved in the system but translation has not been completed. Processing will continue after the network recovery and the history will not be deleted.
```

index-2b (8.24 с):

```text
The original file has been saved by the system, but the translation is not yet complete. Processing will continue after the network is restored; no history will be deleted.
```

index-9b (14.75 с):

```text
The system has saved the original file, but the translation is not yet complete. Processing will resume once the network connection is restored, and no history will be deleted.
```

### en-zh (en → zh)

```text
The backup contains three independent copies. A successful upload does not prove that the file can be restored.
```

Что проверять: Три независимые копии; успешная загрузка не доказывает восстановление.

madlad (7.90 с):

```text
备份包含三个独立的副本,成功上传并不意味着文件可以恢复。
```

index-2b (5.75 с):

```text
备份包含三个独立的副本。成功上传并不意味着文件能够被恢复。
```

index-9b (11.21 с):

```text
备份包含三个独立的副本。上传成功并不意味着该文件可以被恢复。
```

### en-ru-identifier (en → ru)

```text
Call GET /v1/status before deploying version 2.4.1. Do not change the code ERR_TIMEOUT.
```

Что проверять: Сохранить метод, endpoint, версию и код ошибки.

madlad (11.02 с):

```text
Вызовите GET /v1/status перед развертыванием версии 2.4.1. Не меняйте код ERR_TIMEOUT.
```

index-2b (8.71 с):

```text
Выполните запрос GET /v1/status перед развертыванием версии 2.4.1. Не изменяйте код ERR_TIMEOUT.
```

index-9b (15.20 с):

```text
Вызовите GET /v1/status перед развертыванием версии 2.4.1. Не изменяйте код ERR_TIMEOUT.
```

### en-ru-placeholder (en → ru)

```text
Hello, {user_name}. Your request {{request_id}} is pending; please do not submit it again.
```

Что проверять: Оба placeholder сохранены, просьба не отправлять повторно.

madlad (10.99 с):

```text
Здравствуйте, {user_name}. Ваш запрос {{request_id}} отложен; пожалуйста, не отправляйте его повторно.
```

index-2b (8.93 с):

```text
Здравствуйте, {user_name}. Ваш запрос {{request_id}} находится в ожидании; пожалуйста, не отправляйте его повторно.
```

index-9b (14.32 с):

```text
Здравствуйте, {user_name}. Ваша заявка {{request_id}} находится в обработке; пожалуйста, не отправляйте её повторно.
```

### ru-en-list (ru → en)

```text
1. Сохранить документ.
2. Проверить контрольную сумму.
3. Не удалять оригинал.
```

Что проверять: Сохранить три пункта, порядок и отрицание.

madlad (5.37 с):

```text
1. Save the document.
2. Check the checksum.
3. Do not delete the original.
```

index-2b (7.93 с):

```text
1. Save the document.
2. Check the checksum.
3. Do not delete the original.
```

index-9b (14.80 с):

```text
1. Save the document.
2. Check the checksum.
3. Do not delete the original.
```

### en-ru-context (en → ru)

```text
A small manufacturing company moved its plant closer to the port. The new site is not a power plant, and no additional electricity generation is planned.
```

Что проверять: Plant — завод компании, не электростанция; генерация не планируется.

madlad (14.21 с):

```text
Небольшая промышленная компания переместила свой завод ближе к порту. Новый объект не является электростанцией, и никакого дополнительного производства электроэнергии на нем не планируется.
```

index-2b (10.72 с):

```text
Малое производственное предприятие перенесло свой завод ближе к порту. Новый объект не является электростанцией, и дополнительная генерация электроэнергии не планируется.
```

index-9b (18.06 с):

```text
Небольшая производственная компания перенесла свой завод ближе к порту. Новое место не является электростанцией, и никакого дополнительного производства электроэнергии не планируется.
```

### ru-en-context (ru → en)

```text
Сотрудник сообщил об ошибке, но не утверждал, что данные потеряны. До проверки резервной копии нельзя считать восстановление успешным. Даже если сервер отвечает на запросы, очередь может не продвигаться.
```

Что проверять: Не добавлять потерю данных; успешность восстановления и продвижение очереди не следуют из доступности.

madlad (25.76 с):

```text
The employee reported an error, but did not claim that data was lost. Until the backup is checked, the recovery cannot be considered successful. Even if the server responds to requests, the queue may not move.
```

index-2b (11.25 с):

```text
The employee reported an error but did not claim that the data was lost. Recovery cannot be considered successful until the backup has been checked. Even if the server responds to requests, the queue may not advance.
```

index-9b (23.77 с):

```text
The employee reported an error but did not claim that data was lost. Recovery cannot be considered successful until the backup has been verified. Even if the server is responding to requests, the queue may not be progressing.
```

### en-ru-newlines (en → ru)

```text
The first replica is healthy.
The second replica is unavailable.
Do not restart both replicas at the same time.
```

Что проверять: Разные статусы двух реплик и запрет одновременного перезапуска.

madlad (5.71 с):

```text
Первая копия здорова.
Вторая реплика недоступна.
Не перезапускайте обе реплики одновременно.
```

index-2b (7.92 с):

```text
Первая реплика в порядке.
Вторая реплика недоступна.
Не перезапускайте обе реплики одновременно.
```

index-9b (15.72 с):

```text
Первая реплика работает исправно.
Вторая реплика недоступна.
Не перезапускайте обе реплики одновременно.
```

## Воспроизводимость и очистка

Опись Docker на границах вычислительного этапа совпала: `True`. Временные контейнеры удалены; сценарий не создавал сети, тома, задания Kafka или записи рабочих БД. При разрешённой остановке эта сверка начинается уже после паузы: исходное состояние и восстановление вычислителя подтверждаются отдельно в maintenance-result.json.

Версии, SHA-256, параметры запуска, сырые ответы и обе описи сохранены в JSON рядом. Загрузка весов, проверка SHA-256, запуск и прогрев не входят в задержку отдельных переводов.
