# 00-INFRA-20261106-0014-product-readiness-and-site-admission — Готовность продукта и допуск площадки

- **Статус:** Принят
- **Дата:** 2026-11-06
- **Происхождение:** требования независимого выпуска, репозиторные security-gates и матрица готовности инфраструктурных владельцев

## Контекст

Успешные локальные тесты исходников не доказывают готовность неизвестной production-площадки с её PKI, DNS, failure domains, ресурсами и процедурами восстановления.

## Ключевые решения

- `PRODUCTION_READY` продукта означает закрытые код, контракты, документацию, сборку, security gates, backup/restore paths и поддерживаемые профили.
- Допуск конкретной площадки отдельно подтверждает PKI/KMS, DNS/firewall/WAN, capacity, failure domains, alert delivery, backup target, RPO/RTO и immutable image admission.
- Исторический отчёт, health endpoint или успешный build не заменяет свежий bounded test площадки.
- Секреты, production credentials и runtime data не входят в Git и не требуются для репозиторного CI.
- Release закрепляет source commit, dependency versions, digests, inventory и provenance; mutable tags не являются допуском.
- Локальная проверка не даёт полномочия `RESUME`, restart, destructive cleanup или migration production-state.

## Последствия

- Репозиторий может честно объявлять готовность поддерживаемого продукта без ложного заявления о любой площадке.
- Operator получает явный список доказательств, необходимых перед включением нагрузки.
- Непроверенные внешние gates остаются открытыми, а не маскируются статусом кода.

## Проверка и откат

CI проверяет inventory, tests, SAST, dependency audit, image/security evidence, documentation, links, contracts и deterministic bundle. Site checklist формирует свежие immutable evidence. Откат выпуска не удаляет state и требует того же admission для выбранной версии.

## Официальные основания

- [SLSA provenance](https://slsa.dev/spec/v1.1/provenance) определяет проверяемую связь артефакта с исходниками и build process.
- [GitHub artifact attestations](https://docs.github.com/en/actions/security-for-github-actions/using-artifact-attestations) описывают подпись и проверку build provenance.
- [NIST SSDF SP 800-218](https://csrc.nist.gov/pubs/sp/800/218/final) отделяет практики безопасной разработки и выпуска от эксплуатационного risk acceptance.
