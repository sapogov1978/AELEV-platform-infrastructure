# Ячейка внешнего провайдера перевода

> Этот recipe размещает выбранный непрозрачный provider image рядом с worker. Он
> не входит в `full-local`, release BOM или production admission Platform
> Infrastructure и не раскрывает реализацию провайдера.

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
задайте точный образ `name@sha256:...`, маршруты собственной PostgreSQL/Telemetry
Delivery и пути к identities. Создайте игнорируемый файл
соседний игнорируемый `TranslationProvider/.env.local` с правами `0600` уже
указан в шаблоне; вручную замените в нём только значение `OPENAI_API_KEY` и не
передавайте его через Git, журнал или командную строку. После подготовки
остальных параметров отсутствие содержимого этого файла остаётся единственным
локальным блокером выбранного provider runtime. На macOS/Linux используйте:

```sh
sh compute-runtime/bootstrap.sh doctor --env-file compute-runtime/compute.env
sh compute-runtime/bootstrap.sh prepare --env-file compute-runtime/compute.env
sh compute-runtime/bootstrap.sh start --env-file compute-runtime/compute.env
sh compute-runtime/bootstrap.sh status --env-file compute-runtime/compute.env
sh compute-runtime/bootstrap.sh stop --env-file compute-runtime/compute.env
```

На Windows используйте те же действия через `compute-runtime\bootstrap.ps1`. Требуются Python 3.13, локальный Docker в режиме Linux containers и совместимые с архитектурой станции допущенные образы. Provider image обязан сам объявить рабочий `HEALTHCHECK` и слушать `/run/aelev-translator/translator.sock`; без этого `start --wait` закрыто завершается ошибкой. Успешный `doctor` проверяет только структуру поставки и digests, а не качество перевода или внутренности провайдера.
