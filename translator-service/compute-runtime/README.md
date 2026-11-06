# Вычислительная станция Translator

Этот каталог предоставляет тот же внешний жизненный цикл, что вычислительный установщик Advance Screening: `doctor`, `prepare`, `start`, `status` и `stop`. Он запускает только владельческую пару model/worker из [compose.compute.yml](../compose.compute.yml), никогда не запускает API, Kafka consumers, outbox или UI и не управляет удалённым Docker daemon.

Скопируйте [compute.env.example](compute.env.example) в закрытый файл станции, задайте точный образ `name@sha256:...`, маршруты собственной PostgreSQL/Telemetry Delivery и пути к identities. На macOS/Linux используйте:

```sh
sh compute-runtime/bootstrap.sh doctor --env-file compute-runtime/compute.env
sh compute-runtime/bootstrap.sh prepare --env-file compute-runtime/compute.env
sh compute-runtime/bootstrap.sh start --env-file compute-runtime/compute.env
sh compute-runtime/bootstrap.sh status --env-file compute-runtime/compute.env
sh compute-runtime/bootstrap.sh stop --env-file compute-runtime/compute.env
```

На Windows используйте те же действия через `compute-runtime\bootstrap.ps1`. Требуются Python 3.13, локальный Docker в режиме Linux containers и совместимый с архитектурой станции допущенный образ. Успешный `doctor` не подтверждает WAN, TLS, ресурсы, качество модели или аппаратный допуск.
