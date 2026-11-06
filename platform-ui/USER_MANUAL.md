# Работа с оболочкой

Откройте `PUBLIC_URL` из собственного `.env` и войдите через настроенный Keycloak пользователем `platform-operator` с ролью `platform-ui-viewer`. Это единственная обычная человеческая identity операторских UI; её первоначальный единый пароль находится в собственном `secrets/provision/operator-password`, не в документации или чате. Каждый открываемый владелец использует ту же Keycloak SSO-сессию, но отдельные client, audience, token и роли.

Главная показывает названия и состояние, опубликованные владельцами. `Доступен`/`Недоступен` — утверждение автора сообщения в пределах TTL, не заключение оболочки о предметной корректности. Свежий `Недоступен` отображается оранжевым: отдельный компонент может ограничивать функцию, пока API/UI и остальные части сервиса доступны. `Нет данных` означает неизвестное состояние компонента при живом издателе. Если без отписки перестали приходить публикации, строка остаётся с красным `Нет связи`: оболочка не угадывает, выключился контейнер, завис или потерял сеть. При штатной подтверждённой отписке строка исчезает. Без активных инъекций список пуст — скрытого встроенного перечня нет.

В инфраструктурной группе верхнее название отражает назначение, а раскрытие показывает реализующие его продукты и процессы. Владелец задаёт оба уровня в своём описании; оболочка отображает их без встроенных соответствий. При создании нового описания выбирайте название возможности для `title`, названия технологий и частей — для `components[].title`. При переименовании увеличивайте `revision`, сохраняя UUID, identity и последовательность состояния; удаление и повторная регистрация не требуются.

Индикатор слева от кнопки «Состояние» один для всех разделов. В открытом сервисном UI он показывает сообщённые владельцем получение данных, ошибку, время успешного ответа и режим обновления; в странице состояния — получение метаданных оболочкой. Источник указан в подсказке индикатора. Пояснение «Данные обновляются раз в минуту» соответствует интервалу 60 секунд текущего источника. При ошибке успешное время сохраняется, индикатор становится красным; во время запроса отображается получение данных. Новый раздел или перезагруженная страница не наследуют прежний успех; если владелец не прислал сообщение, показаны неизвестные данные и режим. Этот индикатор не заменяет время измерения отдельного сервиса в строке состояния.

Слева, в «Подключенные сервисы», появляются только функциональные сервисы, чей владелец объявил `service_kind=FUNCTIONAL` и хотя бы один раздел. Если раздел один, оператор видит одну кнопку с названием сервиса; отдельная подпись раздела в меню не повторяется. Несколько разделов отображаются отдельными кнопками внутри группы сервиса. Необязательный `navigation_order` задаёт владельцу положение своего модуля по возрастанию; без него используется обычное значение `0`, а максимальное значение помещает модуль после обычных. Инфраструктурные компоненты, функциональные описания без разделов и прежние описания без типа доступны в «Состоянии», но не в этом меню; владелец прежнего функционального UI добавляет тип новой ревизией. Выбор открывает отдельный UI владельца, которому может потребоваться собственный вход. Токен оболочки ему не передаётся; его страницы, метрики и команды остаются в его границе. `EN`/`RU` переключает оболочку и передаёт разрешённый контекст языка странице; `Выход` завершает сессию оболочки, не утверждает завершение всех отдельных сессий владельцев.

Подключение страницы выполняет её владелец по [контракту](../docs/api/platform-ui-injection.md), не оператор путём правки каталога. Настройки, HTTPS, запуск и тесты описаны в [README](README.md). Локальный тестовый сертификат допускается только для проверенного локального адреса; на удалённом узле необходима доверенная цепочка.

## Создание и подключение UI нового сервиса

На странице состояния общий индикатор проверяет полноту присланных отчётов, а не только HTTP 200: при неизвестных состояниях он показывает предупреждение, при `LOST` — потерю связи, при пустом списке — отсутствие активных публикаций. После получения известных состояний показаны «Состояния получены» и время без дополнительной подписи; оно обозначает получение списка API, исходное время наблюдения остаётся в каждой строке. Если текущий источник не прислал новый успех за два заявленных интервала, отметка становится «Обновление задерживается», сохраняя прежнее время. Свежий отчёт `OFFLINE` означает успешно полученные сведения об отказе, не ошибку получения.

Этот раздел предназначен разработчику сервиса: после подключения его название и состояние появятся в оболочке, а функциональные страницы — в левом меню. Менять исходники, env, образ или Compose оболочки для этого не нужно. Все файлы нового UI, его настройки, удостоверения, публикация метаданных и тесты остаются в каталоге владельца. Нормативные правила и ограничения определяет только [контракт инъекции](../docs/api/platform-ui-injection.md); примеры ниже иллюстрируют его, а не создают отдельную модель.

В примерах используются вымышленные адреса `https://shell.invalid`, `https://owner.invalid` и `https://identity.invalid`: замените их действительными HTTPS-адресами своей установки. Пример не регистрируется в работающей оболочке автоматически. Сервис и оболочка могут находиться на разных машинах и в разных странах: нужны маршрутизация, разрешённый HTTPS и доверенные сертификаты, не общий диск или Docker bridge.

### Подготовить собственную поставку

Разместите файлы у владельца, например:

```text
<сервис>/
  Dockerfile
  compose.yml
  .env.example
  .env                    # локальные настройки, не Git
  deployment/
    module.json           # каноническое описание и его ревизия
  ui/
    index.html
    owner-bridge.js
    publish.py
    ui-config.json        # производный публичный файл
  secrets/                # доставка через механизм секретов, не образ и не Git
  test/                   # собственные проверки подключения
```

Это схема размещения, не требование второго backend: используйте уже существующий API владельца. Соберите UI только из своего каталога, включите `test`, `.env`, `secrets` и производные файлы в собственные правила исключения. Описание, последняя ревизия, последовательность состояния и неподтверждённые публикации должны переживать перезапуск в собственном устойчивом хранилище; не храните их только в файловой системе контейнера.

Для примера используется префикс `AELEV_FUNCTIONAL_EXAMPLE_UI_`: `FUNCTIONAL` — назначение, `EXAMPLE_UI` — вымышленный сервис. В реальной поставке замените его своим принятым префиксом, для инфраструктурного компонента — `AELEV_INFRASTRUCTURE_<СЕРВИС>_`. Это настройки владельца, не дополнение к env оболочки. Собственный `.env.example` содержит только несекретные значения и пути доставки секретов:

<!-- example: publisher-env -->
```dotenv
AELEV_FUNCTIONAL_EXAMPLE_UI_SHELL_URL=https://shell.invalid
AELEV_FUNCTIONAL_EXAMPLE_UI_OIDC_URL=https://identity.invalid
AELEV_FUNCTIONAL_EXAMPLE_UI_OIDC_REALM=aelev
AELEV_FUNCTIONAL_EXAMPLE_UI_MODULE_ID=be36a9c0-fbe9-4d14-8b77-3b5fc5c5ca55
AELEV_FUNCTIONAL_EXAMPLE_UI_PUBLISHER_CLIENT_ID=example-ui-publisher
AELEV_FUNCTIONAL_EXAMPLE_UI_PUBLISHER_SECRET_FILE=./secrets/publisher-secret
AELEV_FUNCTIONAL_EXAMPLE_UI_OIDC_CA_FILE=./secrets/oidc-ca.crt
AELEV_FUNCTIONAL_EXAMPLE_UI_SHELL_CA_FILE=./secrets/shell-ca.crt
AELEV_FUNCTIONAL_EXAMPLE_UI_PUBLISHER_CERT_FILE=./secrets/publisher.crt
AELEV_FUNCTIONAL_EXAMPLE_UI_PUBLISHER_KEY_FILE=./secrets/publisher.key
AELEV_FUNCTIONAL_EXAMPLE_UI_POLL_SECONDS=60
```

`SHELL_URL` — точный origin оболочки; `OIDC_URL` и `OIDC_REALM` — адрес выдачи токена, который принимается её API с настроенным issuer; `MODULE_ID` — постоянный UUID модуля, создайте свой один раз командой `python3.13 -c 'import uuid; print(uuid.uuid4())'`. `PUBLISHER_CLIENT_ID` — отдельный машинный клиент. `*_FILE` указывают на доверие и удостоверения только этого владельца. `POLL_SECONDS` задаёт паузу опроса его собственного UI. Относительные пути в примере считаются от каталога сервиса; секретных значений в env нет. В контейнере используйте пути своей доставки файлов, а не пути рабочей станции.

### Выдать издателю права и подготовить HTTPS

Сначала установите оболочку и выполните её одноразовую настройку по [README](PROVIDER_OPERATIONS.md#подготовка-и-запуск). Затем отдельная задача развёртывания владельца либо уполномоченный администратор IdP настраивает собственный машинный клиент:

1. В realm, доверенном API оболочки, создайте отдельный OIDC-клиент издателя с включённой аутентификацией клиента и service account. Для него используется `client_credentials`; интерактивный вход, implicit flow и password grant не нужны.
2. Назначьте его service account только realm-роль `platform-ui-publisher` для публикации. Убедитесь, что роль включена в разрешённую область клиента и попадает в `realm_access.roles`. Не выдавайте ему административные роли или роль просмотра без отдельной необходимости.
3. Добавьте собственный audience mapper: `protocol=openid-connect`, `protocolMapper=oidc-audience-mapper`, `included.client.audience=<KEYCLOAK_CLIENT_ID оболочки>`, `access.token.claim=true`, `id.token.claim=false`. API проверяет `aud`, а не только подпись; аудитория собственного браузерного клиента владельца её не заменяет.
4. Доставьте секрет клиента в `PUBLISHER_SECRET_FILE`. Выдайте издателю отдельное удостоверение с назначением `clientAuth`, подписанное CA, которому доверяет `nginx/publisher-ca.crt` оболочки. Доставьте ему только свою пару сертификат/ключ и публичные CA для HTTPS оболочки и IdP. Закрытый ключ CA и пароль администратора Keycloak в runtime не нужны.
5. Проверьте DNS, SAN, срок действия цепочек и взаимную TLS-аутентификацию с машин издателя. Не используйте `verify=False`, `curl -k`, общий клиентский ключ или самоподписанный сертификат без явно настроенного доверия. Локальный выпуск и продукционный допуск различаются по [00-INFRA-20261106-0007-workload-and-operator-identity](../docs/adr/00-INFRA-20261106-0007-workload-and-operator-identity.md); порядок локальной PKI оболочки описан в [README](PROVIDER_OPERATIONS.md#https-и-файлы-секретов).

Права издателя и права пользователя страницы — разные вещи. Для настоящего защищённого UI владелец создаёт отдельный публичный OIDC-клиент с Authorization Code и PKCE S256, точными redirect URI и origin; его backend проверяет собственные audience и роли. Используйте уже утверждённый OIDC-клиент своего стека. Сессия оболочки не передаётся странице. Вход IdP внутри iframe может быть запрещён: предусмотрите самостоятельное открытие UI или собственный разрешённый поток входа в отдельном окне, не ослабляйте CSP и не копируйте токены оболочки.

### Опубликовать название и разделы

Сохраните у владельца `deployment/module.json`. Один стабильный `module_id` обозначает один модуль; `revision` увеличивается при изменении названия, категории или разделов, не при каждом запуске. Ни HTML, ни исходники страницы оболочке не отправляются:

<!-- example: functional-description -->
```json
{
  "schema_version": 1,
  "module_id": "be36a9c0-fbe9-4d14-8b77-3b5fc5c5ca55",
  "revision": 1,
  "service_kind": "FUNCTIONAL",
  "title": {"ru": "Пример сервиса", "en": "Example service"},
  "sections": [
    {
      "id": "overview",
      "title": {"ru": "Обзор", "en": "Overview"},
      "url": "https://owner.invalid/"
    }
  ]
}
```

Для инфраструктурного компонента достаточно своего названия и состояния без функциональных страниц:

<!-- example: infrastructure-description -->
```json
{
  "schema_version": 1,
  "module_id": "67dfae64-72d4-48b8-beca-48cc8418223b",
  "revision": 1,
  "service_kind": "INFRASTRUCTURE",
  "title": {"ru": "Пример инфраструктуры", "en": "Example infrastructure"},
  "sections": []
}
```

Используйте соответствующий `MODULE_ID` своего владельца. Инфраструктурное описание попадёт в «Состояние», но не в левое меню. Текущая страница состояния показывает название, состояние и время; она ещё не встраивает инфраструктурные панели управления. Поля `state_path`, Docker-команды или дополнительные поля в JSON добавлять нельзя: текущий контракт их не принимает. Собственные административные действия выполняет API владельца с его авторизацией, ожидаемой ревизией, идемпотентностью и аудитом; публикация описания не даёт права останавливать контейнеры.

### Опубликовать ссылку «Открыть»

Локальное исключение по [00-INFRA-20261106-0007-workload-and-operator-identity](../docs/adr/00-INFRA-20261106-0007-workload-and-operator-identity.md) и [00-INFRA-20261106-0007-workload-and-operator-identity](../docs/adr/00-INFRA-20261106-0007-workload-and-operator-identity.md): владелец может опубликовать `sections[].url`, `swagger_url` или `ui_url` с HTTP на точном `localhost`, `127.0.0.1`, `::1`. Это адрес компьютера браузера, не удалённого узла; владелец обязан явно выбрать локальную среду, loopback bind и собственную авторизацию. Сначала обновите API/CSP оболочки, затем публикуйте новую ссылку в большей ревизии. Для отката верните HTTPS в большей ревизии до старого API; UUID, sequence и данные сохраняются. Остальные ограничения и правила ниже действуют также для этого исключения.

Владелец задаёт доступный браузеру HTTPS-адрес либо разрешённый локальный loopback HTTP-адрес в своём описании и увеличивает `revision`. Это не `SHELL_URL`, не адрес внутренней Docker-пробы и не автоматически вычисленный `/docs`. Для инфраструктуры публикуйте `ui_url`, если у продукта действительно есть веб-интерфейс; если его нет, но существует Swagger, публикуйте `swagger_url`. При наличии обоих адресов одна кнопка предпочитает UI. У компонента можно опубликовать собственную ссылку, но ссылка группы ему не наследуется.

Самостоятельный инфраструктурный интерфейс, включая составную часть:

<!-- example: infrastructure-links-description -->
```json
{
  "schema_version": 1,
  "module_id": "67dfae64-72d4-48b8-beca-48cc8418223b",
  "revision": 2,
  "service_kind": "INFRASTRUCTURE",
  "title": {"ru": "Техническая возможность", "en": "Technical capability"},
  "sections": [],
  "ui_url": "https://console.owner.invalid/",
  "components": [
    {"id": "server", "title": {"ru": "Процесс", "en": "Process"}, "ui_url": "https://console.owner.invalid/"},
    {"id": "api", "title": {"ru": "Операционный API", "en": "Operations API"}, "swagger_url": "https://api.owner.invalid/docs"},
    {"id": "publisher", "title": {"ru": "Издатель", "en": "Publisher"}}
  ]
}
```

У функционального сервиса с внедрёнными разделами ссылка на UI в состоянии не публикуется: его страницы уже доступны в левом меню. Добавьте только существующий Swagger; если документации API нет, оставьте оба поля отсутствующими:

<!-- example: functional-swagger-description -->
```json
{
  "schema_version": 1,
  "module_id": "be36a9c0-fbe9-4d14-8b77-3b5fc5c5ca55",
  "revision": 2,
  "service_kind": "FUNCTIONAL",
  "title": {"ru": "Пример сервиса", "en": "Example service"},
  "sections": [{"id": "overview", "title": {"ru": "Обзор", "en": "Overview"}, "url": "https://owner.invalid/"}],
  "swagger_url": "https://api.owner.invalid/docs"
}
```

Сначала обновите API и оболочку, затем собственный издатель с новой ревизией описания. Публичный клиент 1.1.0 уже передаёт эти точные байты и не требует новой версии. Без ссылки, при её снятии новой ревизией или при штатной отписке кнопка исчезает вместе с соответствующим представлением; `UNKNOWN`/«Нет связи» не доказывают недоступность внешнего интерфейса и не убирают диагностическую ссылку. Нажатие открывает отдельную вкладку без токенов оболочки и Referer; пользователь проходит собственную авторизацию целевого приложения. При mTLS установите в браузере собственное разрешённое операторское удостоверение из утверждённой PKI: закрытый ключ издателя использовать нельзя. Не отключайте проверку TLS, mTLS или авторизацию ради открытия ссылки. Правила URL, совместимости и отката — в [каноническом контракте](../docs/api/platform-ui-injection.md#ссылки-на-собственные-интерфейсы).

### Передавать состояние с устойчивой последовательностью

После собственной проверки работоспособности подготовьте `status.json` в своём устойчивом хранилище. Пример показывает форму сообщения, а не свежий результат проверки:

<!-- example: owner-status -->
```json
{
  "sequence": 1,
  "state": "ONLINE",
  "observed_at": "2026-10-10T10:00:00Z"
}
```

`ONLINE` должен отражать результат реальной проверки владельца, `OFFLINE` — подтверждённую недоступность, `UNKNOWN` — недостаток сведений. Для нового наблюдения присвойте следующую устойчиво сохранённую `sequence` и фактическое `observed_at` с временной зоной. После перезапуска не начинайте с единицы; несколько экземпляров одного владельца должны согласованно выделять последовательность. Для сообщения UI-индикатора ниже используется другая, эфемерная последовательность внутри загрузки iframe.

Порядок отправки: описание, затем состояние существующего модуля. До подтверждения сохраните точное содержимое запроса. При сетевой ошибке повторяется то же описание/наблюдение с той же ревизией/последовательностью, а не новое время; подтверждение могло потеряться после записи. Новый факт состояния получает новый номер. Выберите период публикации меньше `STATUS_TTL_SECONDS` оболочки с запасом на задержки; UI-опрос раз в минуту не заменяет эту публикацию. Срок исчисляется от наблюдения, не доставки: задержанное сообщение может сразу показываться как `UNKNOWN`.

Публикации подтверждаются API; новые наблюдения продлевают присутствие, точный повтор старого запроса — нет. Полученный список сообщает отдельно `status.connection` (`CONNECTED`/`LOST`) и серверное время `status.received_at`; это не замена `observed_at` владельца. После потери публикаций без отписки строка остаётся с «Нет связи», даже если чтение списка прошло успешно. Правила определены [контрактом присутствия](../docs/api/platform-ui-injection.md#присутствие-и-штатная-отписка).

При штатном завершении сохраните и подтвердите прежний неподтверждённый запрос, затем отправьте в тот же `/status` новую устойчивую последовательность с `active=false`. Пример формы отписки:

<!-- example: owner-withdrawal -->
```json
{
  "sequence": 3,
  "state": "UNKNOWN",
  "observed_at": "2026-10-10T10:01:00Z",
  "active": false
}
```

Перед отправкой подставьте фактическое время и следующий номер из собственного журнала; сохраните точные байты и намерение отписки до подтверждения. Тем же примером отправителя выполните `python3.13 ui/publish.py status withdrawal.json`. После подтверждения модуль не входит в список, но защита владения и последовательность сохраняются. При следующем запуске опубликуйте новое состояние с `active=true` и большим номером: UUID и `revision` прежние. Отсутствие `active` у прежнего издателя совместимо с активной публикацией; для штатной отписки нужна новая версия издателя. DELETE при обычном выключении не отправляется: это постоянный отзыв представления, после которого нужна большая ревизия. Смена `sub` после пересоздания машинной identity не даёт права переписать прежний модуль.

### Показать состав сервиса в состоянии

Для сервиса из нескольких технических частей владелец публикует необязательный состав в новой ревизии описания. Оболочка не узнаёт его из Docker или имён процессов. Например, следующая декларация заменяет прежнюю инфраструктурную декларацию того же владельца:

<!-- example: component-description -->
```json
{
  "schema_version": 1,
  "module_id": "67dfae64-72d4-48b8-beca-48cc8418223b",
  "revision": 2,
  "service_kind": "INFRASTRUCTURE",
  "title": {"ru": "Пример инфраструктуры", "en": "Example infrastructure"},
  "sections": [],
  "components": [
    {"id": "process", "title": {"ru": "Процесс владельца", "en": "Owner process"}},
    {"id": "store", "title": {"ru": "Хранилище владельца", "en": "Owner store"}}
  ]
}
```

Отчёт имеет одну устойчивую последовательность и отдельные результаты проверок. Пример намеренно показывает отказ хранилища при работающем процессе; фактические состояния и времена должны получаться из проверок владельца:

<!-- example: component-status -->
```json
{
  "sequence": 2,
  "state": "OFFLINE",
  "observed_at": "2026-10-10T10:00:00Z",
  "components": [
    {"id": "process", "state": "ONLINE", "observed_at": "2026-10-10T09:59:59Z"},
    {"id": "store", "state": "OFFLINE", "observed_at": "2026-10-10T10:00:00Z"}
  ]
}
```

Описание отправляется прежней командой публикации, затем весь сохранённый отчёт — прежней командой состояния. Кнопка с названием в «Состоянии» раскрывает состав, повторный щелчок сворачивает его; клавиатурой работают Enter и пробел. Открытая группа сохраняется при обновлении и смене языка. Пропущенный, устаревший или недоставленный результат означает «Нет данных», не зелёное состояние родителя. При штатной остановке издатель подтверждает отписку всего модуля; при аварийном исчезновении отсутствие публикаций выявляется сроком свежести и обозначается как потеря связи. Состав не даёт прав на управление частями и не добавляет их в функциональное меню. Ограничения, подтверждение, миграция и откат определены в [каноническом контракте](../docs/api/platform-ui-injection.md#составные-компоненты).

### Отправить метаданные из Python

Для постоянного издателя доступен публичный клиент `aelev-platform-ui-client==1.1.0`: [описание поставки](client/README.md). Он не является общим образом/процессом и не знает названий сервисов. Включите wheel из `https://<ui-origin>/presentation/client/1.1.0/aelev_platform_ui_client-1.1.0-py3-none-any.whl` в собственный `vendor`, проверьте digest выпуска и закрепите его собственным Poetry lock; сборка затем работает без соседних исходников и сетевого скачивания этого артефакта. Не подключайте библиотеку через путь к каталогу `platform-ui/client`. Версия 1.1.0 подтверждает публикации и отписывается при SIGTERM/SIGINT; обновите сначала API оболочки, затем собственный образ издателя. Прежний пример стандартной библиотеки ниже сохранён как минимальная демонстрация протокола, а не второй вариант журнала/готовой runtime-роли.

Внутри своего сервиса постоянный процесс задаёт только своё описание и собственную пробу. Минимальная схема вызова:

```python
import json
from aelev_platform_ui_client import Settings, aggregate, observe, run

settings = Settings.environment("AELEV_FUNCTIONAL_EXAMPLE_")
description = json.dumps({
    "schema_version": 1, "module_id": settings.module_id, "revision": 1,
    "title": {"ru": "Мой сервис", "en": "My service"},
    "service_kind": "FUNCTIONAL", "sections": [],
    "components": [
        {"id": "server", "title": {"ru": "Собственный процесс", "en": "Own process"}},
        {"id": "publisher", "title": {"ru": "Издатель состояния", "en": "Status publisher"}},
    ],
}, sort_keys=True, separators=(",", ":")).encode()

def own_readiness() -> str:
    # Replace with a real, bounded probe inside the owner's boundary.
    return "UNKNOWN"

def probe() -> tuple[str, list[dict[str, str]]]:
    components = [observe("server", own_readiness)]
    return aggregate(components), components

run(settings, "my-service-status", description, probe)
```

Обязательные суффиксы env: `UI_URL`, `UI_IDENTITY_URL`, `UI_REALM`, `UI_CLIENT_ID`, `UI_AUDIENCE`, `UI_MODULE_ID`, `UI_INTERVAL_SECONDS`, `UI_TIMEOUT_SECONDS`. HTTPS/SAN/CA проверяются всегда. Runtime получает только собственные `shell-ca.crt`, `identity-ca.crt`, `publisher.crt/key`, `client-secret` в `/run/secrets/status` и устойчивый собственный том `/var/lib/aelev-status`. Журнал сохраняется при обновлении/остановке: повтор отправляет прежние точные байты и время; потеря журнала не разрешает сброс sequence под прежним UUID. `health` проверяет локальный цикл, не подменяет готовность всех компонентов. Чужие проверки и Docker socket этому процессу не нужны.

CLI собственного процесса поддерживает `run`, `once`, `health`, `provision`. Последняя — только явно запускаемая одноразовая задача с отдельным mount `/run/secrets/status-admin/{admin-user,admin-password}` и записью собственного `client-secret`; в постоянный процесс административные файлы не монтируются. Она не захватывает client ID/UUID другого владельца и выдаёт только роль публикации, не просмотр/управление Docker. Удостоверение mTLS для разработки/испытаний можно выпустить на доверенной операторской машине командой `python3.13 -m aelev_platform_ui_client.prepare --root <own-status-directory> --ca-cert <approved-client-ca.crt> --ca-key <operator-only-ca.key> --trust shell-ca.crt=<ui-ca.crt> --trust identity-ca.crt=<idp-ca.crt>`; предварительно проверьте `python3.13 --version` и `openssl version`, установку при отсутствии OpenSSL описывает [README](README.md). Она не перезаписывает существующие удостоверения, закрытый ключ CA не копирует в runtime; утверждённая production PKI/SVID и ротация остаются обязательными по 00-INFRA-20261106-0007-workload-and-operator-identity.

При откате на клиент 1.0.0 недостаточно заменить образ: его старая запись SQLite не принимает добавленную колонку `withdrawal`. Сначала оставьте новый API работающим и штатно остановите собственный издатель, подтвердив отписку; сохраните резервную копию собственного журнала без обнуления sequence. Пока все процессы, использующие этот журнал, остановлены, сохраните следующий операторский пример как `ui/downgrade_journal.py` и выполните `python3.13 ui/downgrade_journal.py <путь-к-собственному-outbox.sqlite>` там, где смонтирован только журнал этого владельца. Не применяйте его к SQLite оболочки, чужому тому или работающему издателю. Если отписка или сообщение не подтверждены, пример запрещает откат: сначала восстановите связь и завершите отправку текущим клиентом. Затем верните прежний образ издателя и только после него прежний API; отписка и различение потери связи в старой версии не поддерживаются.

<!-- example: journal-rollback-python -->
```python
import sqlite3
import sys
from pathlib import Path

path = Path(sys.argv[1]).resolve(strict=True)
with sqlite3.connect(path) as journal:
    journal.execute("BEGIN IMMEDIATE")
    columns = {row[1] for row in journal.execute("PRAGMA table_info(state)")}
    if not {"id", "module", "sequence", "pending", "heartbeat"} <= columns:
        raise ValueError("not an owner publication journal")
    if "withdrawal" in columns:
        pending, withdrawing = journal.execute("SELECT pending,withdrawal FROM state WHERE id=1").fetchone()
        if pending is not None or withdrawing:
            raise RuntimeError("unconfirmed publication or withdrawal; rollback forbidden")
        journal.execute("ALTER TABLE state DROP COLUMN withdrawal")
print("Journal compatible with client 1.0.0; identity and sequence preserved")
```

Сохраните следующий пример как `ui/publish.py` внутри своего сервиса. Он использует Python 3.13 и стандартную библиотеку: читает обязательные настройки, получает машинный токен, проверяет HTTPS и предъявляет mTLS. Токен и секрет не передаются в аргументах процесса и не печатаются. Это однократный отправитель сохранённого сообщения, не готовый монитор здоровья или механизм выделения последовательности; его вызывает собственный процесс публикации владельца.

<!-- example: publisher-python -->
```python
import json
import os
import ssl
import sys
from pathlib import Path
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import HTTPSHandler, HTTPRedirectHandler, ProxyHandler, Request, build_opener
from uuid import UUID

PREFIX = "AELEV_FUNCTIONAL_EXAMPLE_UI_"


def setting(name):
    value = os.environ[PREFIX + name]
    if not value:
        raise ValueError("empty setting")
    return value


def https_origin(value):
    url = urlsplit(value)
    if (url.scheme != "https" or not url.hostname or url.username or url.password
            or url.path not in ("", "/") or url.query or url.fragment
            or any(c.isspace() for c in value) or "\\" in value):
        raise ValueError("HTTPS origin required")
    return value.rstrip("/")


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def send(context, url, data, headers, method="POST"):
    opener = build_opener(ProxyHandler({}), HTTPSHandler(context=context), NoRedirect())
    with opener.open(Request(url, data=data, headers=headers, method=method), timeout=20) as response:
        payload = response.read(65537)
        if len(payload) > 65536:
            raise ValueError("oversized response")
        return payload


def main():
    kind, filename = sys.argv[1:]
    if kind not in ("description", "status"):
        raise ValueError("description or status required")
    module_id = setting("MODULE_ID")
    if str(UUID(module_id)) != module_id:
        raise ValueError("canonical UUID required")
    body = Path(filename).read_bytes()
    value = json.loads(body)
    if kind == "description" and value["module_id"] != module_id:
        raise ValueError("module identity mismatch")
    oidc = ssl.create_default_context(cafile=setting("OIDC_CA_FILE"))
    token_url = (https_origin(setting("OIDC_URL")) + "/realms/"
                 + quote(setting("OIDC_REALM"), safe="") + "/protocol/openid-connect/token")
    form = urlencode({"grant_type": "client_credentials",
                      "client_id": setting("PUBLISHER_CLIENT_ID"),
                      "client_secret": Path(setting("PUBLISHER_SECRET_FILE")).read_text().strip()}).encode()
    token = json.loads(send(oidc, token_url, form,
                           {"Content-Type": "application/x-www-form-urlencoded"}))["access_token"]
    target = https_origin(setting("SHELL_URL")) + "/api/modules/" + module_id
    if kind == "status":
        target += "/status"
    transport = ssl.create_default_context(cafile=setting("SHELL_CA_FILE"))
    transport.load_cert_chain(setting("PUBLISHER_CERT_FILE"), setting("PUBLISHER_KEY_FILE"))
    send(transport, target, body,
         {"Content-Type": "application/json", "Authorization": "Bearer " + token}, "PUT")
    print("Publication confirmed")


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print("Publication failed: " + type(error).__name__, file=sys.stderr)
        sys.exit(1)
```

Запускайте из каталога своего сервиса после подготовки файлов и прав; `.env` должен быть вашим доверенным файлом, не присланным скриптом. Загрузка переменных ниже не раскрывает их значения:

```sh
set -a
. ./.env
set +a
python3.13 ui/publish.py description deployment/module.json
python3.13 ui/publish.py status status.json
```

Тот же отправитель применяется к инфраструктурному описанию с собственным префиксом и UUID. Автоматизация владельца сохраняет запрос до отправки, подтверждает после успеха и повторяет временные отказы с ограниченной задержкой. Ошибки доверия, прав, валидации и 409 требуют устранения причины, не бесконечного повтора. Не добавляйте заголовок `X-Verified-Publisher`: в публичном запросе он не является удостоверением, nginx устанавливает его только после проверки сертификата.

### Подключить страницу и общее оформление

Минимальная страница ниже показывает только публичную техническую пробу своего backend. Перед подключением реализуйте у владельца `GET /api/readyz`, возвращающий канонический JSON `{"status":"ready"}` при HTTP 200 и HTTP 503 при неготовности по [общему контракту](../docs/api/health-readiness.md); это вход примера, не требование раскрывать предметные данные. Для предметного UI замените пробу собственным авторизованным чтением и отрисовкой, сохранив браузерный протокол. Пример не реализует вход, доменные формы или управление процессами.

Настройки браузера производны от собственного env. Создайте публичный `ui/ui-config.json` командой из каталога владельца; секреты в него не попадают:

<!-- example: public-config-python -->
```sh
python3.13 - <<'PY' > ui/ui-config.json
import json
import os
prefix = "AELEV_FUNCTIONAL_EXAMPLE_UI_"
print(json.dumps({"shell_origin": os.environ[prefix + "SHELL_URL"],
                  "poll_seconds": int(os.environ[prefix + "POLL_SECONDS"])}))
PY
```

Раздавайте три файла UI на своём HTTPS origin, например `https://owner.invalid/`. CSS не копируется из соседних исходников. SHA-256 поддерживаемого `/presentation/v6.css` закрепляется в коде UI; при новой версии оформления владелец явно обновляет совместимость. Контейнер `.aelev-owner-ui` включает общие примитивы оформления, а локальный CSS задаёт только предметные сетки и специальные состояния. Внешний CSS загружается с Subresource Integrity и `crossorigin=anonymous`: браузер проверяет байты, а не только строку хеша из сообщения.

<!-- example: owner-html -->
```html
<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Пример сервиса</title>
  <script type="module" src="/owner-bridge.js"></script>
</head>
<body class="aelev-owner-ui aelev-page">
  <section class="aelev-panel">
    <h1 id="heading">Пример сервиса</h1>
    <p id="result" class="aelev-muted" role="status">Получение данных…</p>
  </section>
</body>
</html>
```

Сохраните следующий код как `ui/owner-bridge.js`. JavaScript — код страницы в браузере, не инфраструктурный тест или второй backend. Обработчики принимают только точные origin/source и известные поля; `postMessage` не содержит токенов, данных ответа или команд. Страница работает и отдельно: контекст оболочки ей не требуется для чтения своего API.

<!-- example: owner-javascript -->
```javascript
const response = await fetch("/ui-config.json", {cache: "no-store"});
if (!response.ok) throw new Error("Missing UI config");
const config = await response.json();
const shell = new URL(config.shell_origin);
if (shell.protocol !== "https:" || shell.origin !== config.shell_origin
    || !Number.isSafeInteger(config.poll_seconds) || config.poll_seconds <= 0) {
  throw new Error("Invalid UI config");
}
const cssURL = shell.origin + "/presentation/v6.css";
const cssSHA = "16b5dbb04ce93570ca83750d71a14406624be4bd9c840cdd98a7c0e876e33496";
const heading = document.getElementById("heading");
const result = document.getElementById("result");
let connected = false;
let sequence = 0;
let phase = "LOADING";
let updatedAt = null;
let language = "ru";
let ready = null;
let stopped = false;
let timer;
let active;

function render() {
  heading.textContent = language === "ru" ? "Пример сервиса" : "Example service";
  if (phase === "READY") {
    result.textContent = language === "ru"
      ? (ready ? "Backend готов" : "Backend не готов")
      : (ready ? "Backend ready" : "Backend not ready");
  } else {
    result.textContent = language === "ru"
      ? (phase === "ERROR" ? "Ошибка получения данных" : "Получение данных…")
      : (phase === "ERROR" ? "Data request failed" : "Loading data…");
  }
}

function report() {
  if (!connected || stopped) return;
  window.parent.postMessage({type: "AELEV_UI_REFRESH", version: 1,
    sequence: ++sequence, phase, poll_seconds: config.poll_seconds,
    updated_at: phase === "READY" ? updatedAt : null}, shell.origin);
}

function context(event) {
  const data = event.data;
  if (event.source !== window.parent || event.origin !== shell.origin || !data
      || Object.keys(data).sort().join() !== "language,stylesheet_sha256,stylesheet_url,type,version"
      || data.type !== "AELEV_UI_CONTEXT" || data.version !== 1
      || !["ru", "en"].includes(data.language)
      || data.stylesheet_url !== cssURL || data.stylesheet_sha256 !== cssSHA) return;
  connected = true;
  language = data.language;
  document.documentElement.lang = language;
  render();
  report(); // Повтор контекста не сбрасывает последовательность или время.
}

window.addEventListener("message", context);
window.addEventListener("pagehide", () => {
  stopped = true;
  clearTimeout(timer);
  active?.abort();
  window.removeEventListener("message", context);
}, {once: true});
if (window.parent !== window) {
  window.parent.postMessage({type: "AELEV_UI_READY", version: 1}, shell.origin);
}

async function poll() {
  if (stopped) return;
  phase = "LOADING";
  render();
  report();
  active = new AbortController();
  const deadline = setTimeout(() => active.abort(), 15000);
  try {
    const reply = await fetch("/api/readyz", {cache: "no-store",
      credentials: "same-origin", signal: active.signal});
    if (!reply.ok) throw new Error("Readiness request failed");
    const health = await reply.json();
    if (Object.keys(health).length !== 1 || health.status !== "ready") {
      throw new Error("Invalid readiness reply");
    }
    if (stopped) return;
    ready = true;
    updatedAt = new Date().toISOString();
    phase = "READY";
  } catch {
    if (stopped) return;
    phase = "ERROR";
  } finally {
    clearTimeout(deadline);
    if (!stopped) {
      render();
      report();
      timer = setTimeout(poll, config.poll_seconds * 1000);
    }
  }
}

const stylesheet = document.createElement("link");
stylesheet.rel = "stylesheet";
stylesheet.href = cssURL;
stylesheet.crossOrigin = "anonymous";
stylesheet.integrity = "sha256-" + btoa(String.fromCharCode(
  ...cssSHA.match(/../g).map(byte => parseInt(byte, 16))));
try {
  await new Promise((resolve, reject) => {
    stylesheet.onload = resolve;
    stylesheet.onerror = reject;
    document.head.append(stylesheet);
  });
  await poll();
} catch {
  result.textContent = "Ошибка проверки оформления / Stylesheet verification failed";
  phase = "ERROR";
  report();
}
```

Начало запроса сообщает `LOADING`, подтверждённое получение — `READY` с фактическим временем, ошибка — `ERROR` с `updated_at=null`. Даже ответ `ready=false` может быть успешно получен: индикатор отражает получение данных UI, а техническая доступность передаётся отдельным сообщением состояния. Пауза 60 секунд начинается после завершения запроса, поэтому медленная сеть не создаёт параллельную очередь. При закрытии iframe отменяются чтение и таймер; новая загрузка создаёт новую последовательность. Несовместимые стиль или контекст не получают запасного доверия.

HTTPS-сервер владельца должен отдавать CSP как HTTP-заголовок, не как `meta`. Минимум для этой публичной технической страницы с её собственным API и внешним проверяемым CSS:

```text
Content-Security-Policy: default-src 'self'; script-src 'self'; style-src 'self' https://shell.invalid; connect-src 'self'; frame-ancestors https://shell.invalid; object-src 'none'; base-uri 'self'; form-action 'self'
```

Замените origin в заголовке на свой `SHELL_URL`. Для собственного OIDC разрешайте только необходимые точные адреса IdP в соответствующих директивах своего UI. Не выставляйте противоречащий `X-Frame-Options: DENY/SAMEORIGIN`, если страница должна встраиваться на другом origin. Оболочка оставляет sandbox: разрешены скрипты, формы, собственный origin и отдельные окна, но не навигация верхнего окна или доступ к его DOM. При отказе цепочки HTTPS страница не откроется ни через правильный JSON, ни через `postMessage`.

### Проверить подключение

1. Откройте собственный HTTPS UI отдельно: проверьте доверие, свой вход для защищённой страницы, чтение своего API и отсутствие запросов к чужим предметным API.
2. Опубликуйте описание, затем свежее состояние тем же издателем. Оба PUT должны подтвердиться; повтор тех же сообщений должен быть идемпотентен. Изменённое описание требует новой `revision`, новое наблюдение — новой `sequence`.
3. Войдите в оболочку с ролью `platform-ui-viewer`. После следующего успешного опроса метаданных название появится в «Состоянии». Только `FUNCTIONAL` с разделами появится в «Подключенных сервисах»; пересборка оболочки не нужна.
4. Откройте раздел: проверьте отдельный origin iframe, применение CSS, RU/EN, один общий индикатор слева от «Состояния», получение, ошибку и восстановление. При переключении страницы прежнее время успеха не переносится, фоновые метаданные оболочки не перехватывают индикатор владельца.
5. В собственном `test` проверьте отрицательные случаи: отсутствующие mTLS/роль/audience, чужой субъект, конфликтующие номера, HTTP или origin оболочки в URL, чужой источник `postMessage` и неподдерживаемый digest. При штатной остановке проверьте подтверждённую отписку и исчезновение строки; при зависании без отписки — сохранение строки с `LOST` после серверного TTL. Проверьте возврат под прежним UUID с большим номером и восстановление общего индикатора. `UNKNOWN` компонента при живом издателе не должен показываться зелёным успехом получения состояний.
6. Удаляйте только созданные проверкой записи и удостоверения, сохраняя исходные ресурсы. DELETE оставляет маркер отзыва: для полностью изолированного теста используйте собственную временную копию хранилища/API, а не заполняйте рабочую оболочку фиктивными модулями.

Примеры этого раздела извлекаются прямо из мануала и проверяются в [test/test_onboarding.py](test/test_onboarding.py) и [test/onboarding.test.ts](test/onboarding.test.ts): нет второй копии учебного кода. Запуск из каталога `platform-ui` — `python3 test/checks.py`; он использует собственный закреплённый Python 3.13 и также выполняет проверки frontend. Краткий результат примеров — [test/onboarding-result.md](test/onboarding-result.md), общий результат — [test/checks-result.md](test/checks-result.md). Проверки примеров используют временное хранилище и подменённые транспорт/браузер, не регистрируют настоящего потребителя; реальный OIDC/TLS и браузерный жизненный цикл поставки отдельно описаны в [README](PROVIDER_OPERATIONS.md#проверки).

### Разобрать ошибку подключения

| Симптом | Что проверить у владельца |
|---|---|
| 401 | Подпись, сроки access token, точные issuer/audience. ID token не заменяет access token. |
| 403 | Realm-роль издателя, предъявленный mTLS и прежний `sub` владельца UUID. |
| 409 | Сохранённые revision/sequence и содержимое неподтверждённого запроса; не меняйте время при повторе. |
| 413 / 422 | Размер, поля и типы по контракту, уникальность разделов, UUID и отдельный HTTPS origin. |
| 503 | Доверие IdP или собственное хранилище/лимиты оболочки; повторяйте сохранённый запрос только после восстановления. |
| Есть в состоянии, нет в меню | Явный `FUNCTIONAL` и непустые sections; смена типа требует новой ревизии. |
| Нет свежего состояния | Собственная проверка/публикация, порядок отправки, sequence, часы и TTL; UI-индикатор не заменяет состояние. |
| Пустой iframe или запрет входа | HTTPS/SAN, CSP frame-ancestors, X-Frame-Options, собственный OIDC и ограничения браузера. |
| Не применились стили | Доступность точной версии CSS, SHA-256/SRI, CORS и CSP style-src. |
| Индикатор неизвестен | READY/CONTEXT, точные origin/source, поддерживаемый digest и корректные AELEV_UI_REFRESH с растущей sequence. |

## Обновление журнала издателя

Сборка собственного издателя использует закреплённую SQLite 3.53.4; ОС, Python и SDK не заменяются. До первого запуска задайте `AELEV_INFRASTRUCTURE_PLATFORM_UI_UI_RESTART_POLICY=no`. При обновлении сначала удержите точного предшественника через [существующий ограничитель](../USER_MANUAL.md#ограниченное-восстановление-по-unhealthy), остановите только издателя и сохраните полный его том с WAL, inspect и журналы. Не вызывайте отписку, не удаляйте том и не меняйте UUID.

На изолированных копиях проверьте целостность, схему и все строки старым и новым движками. Запускайте только проверенный издатель с прежними mounts и `restart=no`, без запуска зависимостей. После healthy передайте прежний бюджет штатным `replace`; только для действительно незарегистрированной истории с подтверждёнными нулевыми рестартами допустим `enroll`. Значение env оставьте `no`: фактический оставшийся бюджет выставляет ограничитель, а новая инкарнация должна пройти передачу истории снова. Исчерпанный или заблокированный бюджет не сбрасывайте. Перед следующей заменой повторяйте эту процедуру, а не обычный пересоздающий `up`.

## Лёгкая проба издателя

При обновлении сохраняйте фактическое имя Compose-проекта работающего издателя и передавайте его явно через `-p`. Имя самостоятельной группы из примера не подставляется вместо действующей общей группы: два издателя одного журнала одновременно запрещены.

Соберите собственный совместимый образ с закреплённым клиентом Platform UI 1.1.1. Закрепите его в `AELEV_INFRASTRUCTURE_PLATFORM_UI_STATUS_IMAGE` и проверьте Compose. До адресного обновления только `platform-ui-status-publisher` сохраните согласованную SQLite-копию собственного журнала и конфигурацию; не удаляйте том, UUID, sequence или ожидающие байты. Одной замены команды healthcheck без совместимого образа недостаточно. Применяйте `up -d --no-deps --no-build --pull never` только к издателю с профилем `ui`, затем проверьте свежий heartbeat, продвижение sequence и доставку состояния. Нативные процессы, данные, секреты и бюджеты восстановления не меняются. Новый обязательный `AELEV_INFRASTRUCTURE_PLATFORM_UI_STATUS_IMAGE` выбирает только издатель; `AELEV_INFRASTRUCTURE_PLATFORM_UI_API_IMAGE` сохраняет прежний образ API. Перед обновлением существующей конфигурации задайте новый параметр явно: скрытого резервного образа нет. Точные свойства пробы определены в [SPEC](SPEC.md); её успех не заменяет проверки готовности и доставки.

При откате остановите только новый издатель и выберите сохранённый совместимый образ с прежней командой пробы, не меняя журнал. Схема журнала этим обновлением не меняется; возврат более старого клиента с несовместимым форматом без отдельной подготовки не допускается. Если процесс не восстановился, сохраняйте журналы и безопасную причину ошибки, не обнуляйте последовательность и не увеличивайте таймаут вместо устранения причины.

## Официальные руководства

- [Nginx admin guide](https://docs.nginx.com/nginx/admin-guide/) и [SSL termination](https://docs.nginx.com/nginx/admin-guide/security-controls/terminating-ssl-http/) — статическая оболочка, reverse proxy и TLS.
- [FastAPI deployment concepts](https://fastapi.tiangolo.com/deployment/concepts/) — HTTPS, workers, рестарты и запуск API.
- [OAuth 2.0 Authorization Code with PKCE](https://www.rfc-editor.org/rfc/rfc7636) и [OpenID Connect Core](https://openid.net/specs/openid-connect-core-1_0.html) — браузерный вход и проверка identity.
- [HTML iframe sandbox](https://html.spec.whatwg.org/multipage/iframe-embed-object.html#attr-iframe-sandbox) и [Web Messaging](https://html.spec.whatwg.org/multipage/web-messaging.html) — изоляция владельческих UI и точный `postMessage`-канал.
- [SQLite WAL](https://www.sqlite.org/wal.html) и [backup API](https://www.sqlite.org/backup.html) — локальное техническое состояние API и издателя.
