# Проверки исходников, артефактов и образов

Этот реестр относится к Platform Infrastructure. [CI](../../.github/workflows/audit.yml) и [официальные основания решений](../references/official-sources.md) не требуют нормативных документов потребителя.

| Проверка | Исполняемый источник | Что подтверждается |
|---|---|---|
| Состав и границы | [CI inventory](../../tools/ci_inventory.py), [boundary](../../tools/audit_boundary.py), [documentation](../../tools/check_documentation.py) | Покрытие проектов, владельческие границы, самостоятельная документация |
| Секреты и SAST | [Secret scan](../../tools/check_secrets.py), Bandit и Semgrep в CI | Запрещённый материал и проверяемые классы ошибок исходников |
| Закреплённые зависимости | [Supply-chain gate](../../tools/check_supply_chain.py), Poetry locks и UI lock | Точные артефакты и целостность; UI job также выполняет `npm audit` |
| Конечный пользователь образа | [Container gate](../../tools/check_container_security.py), [точные исключения](../audit/container-security-exceptions.json) | Явный non-root USER либо пересмотренное ограниченное исключение |
| Фактический образ Platform UI | [Сборка](../../tools/build_ci_ui_image.py), [image evidence](../../tools/image_evidence.py) | Image ID, SBOM и High/Critical Trivy gate на реально собранном образе |
| Полная сборка и функции | [Clean-room runbook](../operations/clean-room-reproduction.md) | Preflight/BOM, exact source, реальные lifecycle-проверки, cleanup и независимый verifier |
| Исходный release ZIP | [Детерминированный bundler](../../tools/build_bundle.py), [процедура выпуска](../operations/release.md) | Один source commit, version, SHA-256, опись путей и отсутствие локального состояния |

Image evidence Platform UI является доказательством конкретного образа и своего сканирования, не всего набора runtime images. Inventory, успешный lock check и отсутствие Dependabot alerts не заменяют сканирование каждого артефакта на целевой площадке. Подпись, admission готовых production images, классификация findings и допустимые исключения проверяются отдельно для точных digest. Принятые OS/dependency lines не меняются молча ради прохождения проверки.

Релиз исходников не становится подписанным бинарным image-релизом. В `full-local` shared sender и delivery входят только под закреплёнными digest с собственными SBOM/provenance; локально собранные образы фиксируются в release BOM. Consumer закрепляет release ZIP, source commit и опись и не получает право автоматически обновить работающую установку.

## Официальные основания

- [GitHub releases](https://docs.github.com/en/repositories/releasing-projects-on-github/managing-releases-in-a-repository) — точные версии и assets.
- [SLSA provenance v1.1](https://slsa.dev/spec/v1.1/provenance) — связь артефактов с исходниками и процессом сборки.
