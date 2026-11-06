# Development Conventions

Source code and identifiers use English. Russian documentation uses ordinary Russian prose while retaining exact protocol, schema, topic, and product identifiers where needed. Markdown prose paragraphs occupy one physical line.

Python services use Python 3.13, Poetry 2.2.1, Black, Ruff, strict typing, and Pytest. Frontends use their committed Node package manifests and locks. Docker images, operating-system families, and dependency lines remain pinned unless a reviewed compatibility change explicitly replaces them.

Every service owns one bounded responsibility and remains independently buildable, testable, documented, and deployable. Shared behavior is distributed as versioned digest-pinned artifacts; sibling source trees, databases, sockets, and application volumes are not dependency interfaces.

Every repository and owner directory uses uppercase `INDEX.md` as the agent navigation map complementing Graphify. It gives a high-level tabular answer to “what to find where”; it does not replace `AGENTS.md`, `README.md`, `SPEC.md`, `USER_MANUAL.md`, ADRs, or executable configuration. Lowercase `index.md` is not used for active agent maps.

PostgreSQL owns server-side transactional domain state. SQLite is limited to one owner's local technical journals, checkpoints, caches, sequences, and single-node infrastructure metadata. Durable SQLite journals use WAL and `synchronous=FULL` and require a verified backup/restore path.

Inter-service domain exchange uses versioned Kafka CloudEvents with explicit owner, topic, partition key, compatibility policy, and provenance. Consumers are idempotent and commit offsets only after their local durable transaction.

Each repository change updates root `CHANGELOG.md`. Behavior changes also update the owning architecture or API document, affected service documentation, diagrams, tests, and an ADR when a cross-owner decision changes.

Repository-owned architecture decisions use the filename and identifier pattern `00-INFRA-YYYYMMDD-NNNN-essence`, where `essence` is a concise English kebab-case description. An ADR records one current architectural choice as context, a short list of key decisions, consequences, verification/rollback, and official grounds. ADR indexes are decision maps, not chronological change logs; implementation history belongs only in `CHANGELOG.md`. Imported immutable ADRs retain their original identity and path as provenance and are not renamed or rewritten.
