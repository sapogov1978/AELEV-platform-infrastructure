# Development Conventions

Source code and identifiers use English. Russian documentation uses ordinary Russian prose while retaining exact protocol, schema, topic, and product identifiers where needed. Markdown prose paragraphs occupy one physical line.

Python services use Python 3.13, Poetry 2.2.1, Black, Ruff, strict typing, and Pytest. Frontends use their committed Node package manifests and locks. Docker images, operating-system families, and dependency lines remain pinned unless a reviewed compatibility change explicitly replaces them.

Every service owns one bounded responsibility and remains independently buildable, testable, documented, and deployable. Shared behavior is distributed as versioned digest-pinned artifacts; sibling source trees, databases, sockets, and application volumes are not dependency interfaces.

PostgreSQL owns server-side transactional domain state. SQLite is limited to one owner's local technical journals, checkpoints, caches, sequences, and single-node infrastructure metadata. Durable SQLite journals use WAL and `synchronous=FULL` and require a verified backup/restore path.

Inter-service domain exchange uses versioned Kafka CloudEvents with explicit owner, topic, partition key, compatibility policy, and provenance. Consumers are idempotent and commit offsets only after their local durable transaction.

Each repository change updates root `CHANGELOG.md`. Behavior changes also update the owning architecture or API document, affected service documentation, diagrams, tests, and an ADR when a cross-owner decision changes.
