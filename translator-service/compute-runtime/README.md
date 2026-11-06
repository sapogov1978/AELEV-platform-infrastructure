# Ячейка внешнего провайдера перевода

Этот каталог соединяет внешний provider image и принадлежащий Translator worker через `doctor`, `prepare`, `start`, `status` и `stop`. Реализация провайдера в репозиторий не входит: `translator-service` передаёт ему только текст и языки и получает строку перевода либо ограниченную ошибку. Compose не переопределяет `ENTRYPOINT`, `CMD`, переменные или `HEALTHCHECK` provider image. Поэтому запуск, внутренняя готовность, модель, словарь, внешний API либо человек полностью принадлежат поставщику провайдера. Каталог никогда не запускает API, Kafka consumers, outbox или UI и не управляет удалённым Docker daemon.

Скопируйте [compute.env.example](compute.env.example) в закрытый файл станции, задайте точный образ `name@sha256:...`, маршруты собственной PostgreSQL/Telemetry Delivery и пути к identities. На macOS/Linux используйте:

```sh
sh compute-runtime/bootstrap.sh doctor --env-file compute-runtime/compute.env
sh compute-runtime/bootstrap.sh prepare --env-file compute-runtime/compute.env
sh compute-runtime/bootstrap.sh start --env-file compute-runtime/compute.env
sh compute-runtime/bootstrap.sh status --env-file compute-runtime/compute.env
sh compute-runtime/bootstrap.sh stop --env-file compute-runtime/compute.env
```

На Windows используйте те же действия через `compute-runtime\bootstrap.ps1`. Требуются Python 3.13, локальный Docker в режиме Linux containers и совместимые с архитектурой станции допущенные образы. Provider image обязан сам объявить рабочий `HEALTHCHECK` и слушать `/run/aelev-translator/translator.sock`; без этого `start --wait` закрыто завершается ошибкой. Успешный `doctor` проверяет только структуру поставки и digests, а не качество перевода или внутренности провайдера.
