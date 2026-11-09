# Проверка и публикация исходного выпуска

Этот процесс выпускает `sapogov1978/AELEV-platform-infrastructure` с плоской структурой пакета формата 2. Он сохраняет сервисные версии и интерфейсы, а номер инфраструктурного выпуска задают согласованные `release.json` и `full-local.json`. Для 1.0 используется tag `v1.0.0`; API установки остаётся версии 1, формат пакета — версии 2.

## Условия публикации

1. Действующая документация не зависит от репозитория потребителя; README/SPEC/USER_MANUAL/INDEX и диаграммы всех 15 владельцев связаны. Нормативные ADR остаются ключевыми решениями инфраструктуры; исторические материалы сохраняются отдельно.
2. Чистый exact source commit проходит весь [CI](../../.github/workflows/audit.yml), включая документацию, ссылки, границы, секреты, артефакты и тесты.
3. Тот же commit проходит новый полный [clean-room gate](clean-room-reproduction.md): все функции текущего `full-local.json` (для 1.1.1 — 12/12), чистый baseline, cleanup, `source_dirty=false`, BOM и независимый verifier PASS. Старое evidence другой версии не допускается.
4. Release ZIP и lock собраны из этого commit, проверены по SHA-256 и каждой записи описи. Извлечённая поставка имеет исполняемый вход установки и корректный `plan`; локальные env, secrets, runtime, `.codex` и Graphify не попадают в пакет.
5. В assets сохраняются source ZIP/lock, BOM, evidence с артефактами, независимый verification и `release-admission.json`, связывающий version, source, CI/gate run IDs и digest файлов.

Пока хотя бы одно условие не подтверждено, релиз не публикуется как стабильный 1.0. Сам release не разрешает обновление работающей установки или возобновление обработки.

## Сборка и передача

```sh
git status --porcelain
python3 tools/build_bundle.py --output dist/AELEV-platform-infrastructure-1.0.0.zip
```

Bundler читает один committed Git tree и его `release.json`, а не локальные незакоммиченные файлы. Рядом создаётся `.lock.json` с `source_commit`, `archive_sha256` и описью; `archive_url` при публикации назначается точному asset URL без изменения ZIP. Потребитель получает этот lock и проверяет архив до установки. Локальная форма `archive_url=null` является промежуточным build-результатом, не опубликованной ссылкой.

На GitHub сначала создаётся draft с полным 40-символьным target commit, затем прикладываются проверенные assets. После сверки tag/source, имён, digest и содержимого draft публикуется как стабильный release. Старые tags и assets не переписываются. [Официальная процедура GitHub](https://docs.github.com/en/repositories/releasing-projects-on-github/managing-releases-in-a-repository) рекомендует такой порядок и для immutable releases.

## Область Production Ready

Допуск относится к поддерживаемому `full-local` программному продукту и точному source commit, указанному в release evidence. Это исходный выпуск для воспроизводимой сборки, а не подписанный комплект готовых production images. Конкретная площадка отдельно подтверждает PKI/KMS/SVID, DNS/firewall/WAN, независимые failure domains, capacity, backup/PITR, RPO/RTO, image admission и alert delivery. Выпуск 1.1.1 закрепляет выбранные opaque provider/worker images и проверяет их health/UDS-контракт без inference; доступ к внешнему API, quota и ограниченный live smoke остаются отдельным допуском площадки. Сохранность технической очереди Translator проверяется независимо и не разрешает `RESUME`.
