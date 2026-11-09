# Ячейка внешнего провайдера перевода

> Этот recipe размещает выбранный непрозрачный provider image рядом с worker.
> Root/full-local и release BOM используют тот же recipe с exact image digests;
> это не раскрывает реализацию и само по себе не является production admission.

Этот каталог соединяет внешний provider image и принадлежащий Translator worker
через `doctor`, `prepare`, `start`, `status` и `stop`. Реализация провайдера в
репозиторий не входит: `translator-service` передаёт ему только текст и языки и
получает строку перевода либо ограниченную ошибку. Compose не переопределяет
`ENTRYPOINT`, `CMD`, переменные или `HEALTHCHECK` provider image. Он предоставляет
только исходящую сеть без опубликованных портов, UDS volume и generic read-only
secret file `/run/secrets/provider/credential.env`. Модель, prompt, endpoint и способ
исполнения остаются у поставщика провайдера. Каталог не запускает Kafka
consumers, outbox или UI и не управляет удалённым Docker daemon.

Скопируйте [compute.env.example](compute.env.example) в закрытый файл станции,
задайте точные образы `name@sha256:...`, маршруты собственной PostgreSQL/Telemetry
Delivery, их external network flags и пути к identities. В корневом локальном
стеке эти две сети принадлежат Translator и создаются самим Compose; отдельная
compute-станция подключается к уже маршрутизируемым owner networks. Соседний
игнорируемый
`TranslationProvider/.env.local` с правами `0600` служит источником для
create-only импорта в `translator-service/runtime/provider-credential.env`;
вручную задайте в нём `OPENAI_API_KEY` и не передавайте значение через Git,
журнал или командную строку. На macOS/Linux используйте:

```sh
sh compute-runtime/bootstrap.sh doctor --env-file compute-runtime/compute.env
sh compute-runtime/bootstrap.sh prepare --env-file compute-runtime/compute.env
sh compute-runtime/bootstrap.sh start --env-file compute-runtime/compute.env
sh compute-runtime/bootstrap.sh status --env-file compute-runtime/compute.env
sh compute-runtime/bootstrap.sh stop --env-file compute-runtime/compute.env
```

На Windows используйте те же действия через `compute-runtime\bootstrap.ps1`. Требуются Python 3.13, локальный Docker в режиме Linux containers и совместимые с архитектурой станции допущенные образы. Provider image обязан сам объявить рабочий `HEALTHCHECK` и слушать `/run/aelev-translator/translator.sock`; без этого `start --wait` закрыто завершается ошибкой. Успешный `doctor` проверяет только структуру поставки и digests, а не качество перевода или внутренности провайдера.
