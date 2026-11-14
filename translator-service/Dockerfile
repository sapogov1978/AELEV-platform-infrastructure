ARG AELEV_INFRASTRUCTURE_TRANSLATOR_HEALTH_SEED_IMAGE=scratch
FROM python:3.13.7-slim-bookworm AS python-bookworm

# Keep the accepted Python 3.13 / Debian 12 stack while applying the security
# updates published for Bookworm after the immutable upstream image was built.
RUN apt-get update \
    && apt-get upgrade --yes \
    && rm -rf /var/lib/apt/lists/*

FROM python-bookworm AS swagger-assets
# Official immutable Swagger UI release; no browser-time CDN dependencies.
ADD --checksum=sha256:050bc415ee7048dcd881682678f720264e7da5e373f7461d7c58c755305255f7 https://raw.githubusercontent.com/swagger-api/swagger-ui/v5.33.1/dist/swagger-ui-bundle.js /assets/swagger-ui-bundle.js
ADD --checksum=sha256:1ac324f7dcd27e4b9386b4bd6421271ec147e922a22c05ba24b11515e9aa6321 https://raw.githubusercontent.com/swagger-api/swagger-ui/v5.33.1/dist/swagger-ui.css /assets/swagger-ui.css
ADD --checksum=sha256:a5c8a34e09e49737382cc5019446eec7c3db3461963fc5bfe448361d3c5549e9 https://raw.githubusercontent.com/swagger-api/swagger-ui/v5.33.1/dist/oauth2-redirect.js /assets/oauth2-redirect.js
ADD --checksum=sha256:cfc7749b96f63bd31c3c42b5c471bf756814053e847c10f3eb003417bc523d30 https://raw.githubusercontent.com/swagger-api/swagger-ui/v5.33.1/LICENSE /assets/LICENSE
RUN chmod 644 /assets/swagger-ui-bundle.js /assets/swagger-ui.css /assets/oauth2-redirect.js /assets/LICENSE

# Owner-local journals use a pinned compatible SQLite ABI, not Debian's older ABI.
FROM python-bookworm AS sqlite-builder
RUN apt-get update && apt-get install --yes --no-install-recommends gcc libc6-dev \
    && rm -rf /var/lib/apt/lists/*
ADD --checksum=sha256:0e9483900e92cd5de8fd48d16bf9200145a61f7fd5be542a5ac81d8a9516eb9c https://sqlite.org/2026/sqlite-autoconf-3530400.tar.gz /sqlite.tar.gz
RUN mkdir /sqlite-source /sqlite \
    && tar -xzf /sqlite.tar.gz --strip-components=1 -C /sqlite-source \
    && python -c 'import hashlib; from pathlib import Path; assert hashlib.sha3_256(Path("/sqlite-source/sqlite3.c").read_bytes()).hexdigest() == "67f423e9ebbbdc473cbc4772c872ee6b89f31fde4ed0279a5c25d5f65c043a16"' \
    && gcc -shared -fPIC -O1 -DSQLITE_THREADSAFE=1 \
       -DSQLITE_ENABLE_COLUMN_METADATA -DSQLITE_ENABLE_FTS5 -DSQLITE_ENABLE_RTREE \
       -Wl,-soname,libsqlite3.so.0 /sqlite-source/sqlite3.c \
       -o /sqlite/libsqlite3.so.0 -lpthread -ldl -lm

FROM python-bookworm AS poetry-base
ENV POETRY_VERSION=2.2.1 \
    POETRY_NO_INTERACTION=1 \
    POETRY_VIRTUALENVS_IN_PROJECT=1
WORKDIR /opt/aelev
RUN --mount=type=cache,id=aelev-translator-pip,target=/root/.cache/pip \
    pip install "poetry==${POETRY_VERSION}"
COPY pyproject.toml poetry.lock ./
COPY vendor ./vendor


FROM poetry-base AS dependencies
RUN --mount=type=cache,id=aelev-translator-poetry,target=/root/.cache/pypoetry \
    poetry install --only main --no-root --no-ansi

FROM dependencies AS test
RUN apt-get update \
    && apt-get install --yes --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*
RUN --mount=type=cache,id=aelev-translator-poetry,target=/root/.cache/pypoetry \
    poetry install --only main,dev --no-root --no-ansi
ENV PYTHONPATH=/opt/aelev/src AELEV_TRANSLATION_CONTRACT_ROOT=/opt/aelev/contracts \
    VIRTUAL_ENV=/opt/aelev/.venv PATH=/opt/aelev/.venv/bin:$PATH
COPY src ./src
COPY --from=translator_tests . ./test
COPY typings ./typings
COPY contracts ./contracts
COPY alembic.ini ./alembic.ini
COPY alembic ./alembic
COPY deployment ./deployment
COPY Dockerfile ./Dockerfile
COPY prepare-local.py compose*.yml .env.example *.md ./
COPY compute-runtime/*.py compute-runtime/*.example compute-runtime/*.md ./compute-runtime/
COPY compute-runtime/bootstrap.sh compute-runtime/bootstrap.ps1 ./compute-runtime/
COPY --from=swagger-assets /assets ./swagger-assets
CMD ["poetry", "run", "pytest", "-q"]

FROM python-bookworm AS status-runtime
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONPATH=/opt/aelev/src \
    VIRTUAL_ENV=/opt/aelev/.venv PATH=/opt/aelev/.venv/bin:$PATH \
    LD_LIBRARY_PATH=/opt/aelev/sqlite
COPY --from=sqlite-builder /sqlite /opt/aelev/sqlite
RUN python -S -c 'import sqlite3; assert sqlite3.sqlite_version == "3.53.4"'
RUN groupadd --system --gid 10001 aelev \
    && useradd --system --uid 10001 --gid aelev --home-dir /nonexistent aelev \
    && install -d --owner=10001 --group=10001 /var/lib/aelev-status
WORKDIR /opt/aelev
COPY --from=dependencies /opt/aelev/.venv ./.venv
COPY --from=dependencies /opt/aelev/.venv/lib/python3.13/site-packages/aelev_platform_ui_client/health.py ./publisher-health.py
COPY src ./src
COPY contracts ./contracts
USER 10001:10001
ENTRYPOINT ["python", "-m", "translator_service.status_publisher"]
CMD ["run"]

FROM python-bookworm AS translator-core-runtime
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONPATH=/opt/aelev/src \
    VIRTUAL_ENV=/opt/aelev/.venv PATH=/opt/aelev/.venv/bin:$PATH \
    HOME=/tmp XDG_CACHE_HOME=/tmp LD_LIBRARY_PATH=/opt/aelev/sqlite
COPY --from=sqlite-builder /sqlite /opt/aelev/sqlite
RUN python -S -c 'import sqlite3; assert sqlite3.sqlite_version == "3.53.4"'
RUN groupadd --system --gid 10001 aelev \
    && useradd --system --uid 10001 --gid aelev --home-dir /nonexistent aelev \
    && install -d --owner=10001 --group=10001 /run/aelev-translator /var/lib/aelev-status \
    && install -d --owner=10001 --group=10001 --mode=0700 /run/secrets/compute-provider
WORKDIR /opt/aelev
COPY --from=dependencies /opt/aelev/.venv ./.venv
RUN python -m pip uninstall --yes pip \
    && rm -rf /usr/local/lib/python3.13/ensurepip/_bundled
COPY --from=swagger-assets /assets ./swagger-assets
COPY src ./src
COPY contracts ./contracts
COPY alembic.ini ./alembic.ini
COPY alembic ./alembic
USER 10001:10001
CMD ["python", "-m", "translator_service.api_server"]

# Health-only maintenance keeps the exact interpreter and dependencies.
FROM ${AELEV_INFRASTRUCTURE_TRANSLATOR_HEALTH_SEED_IMAGE} AS translator-health-update
ARG AELEV_INFRASTRUCTURE_TRANSLATOR_HEALTH_SEED_IMAGE
# A local snapshot tag is admitted only after checking its exact image ID and
# all inherited layers before and after the build; it is not a registry digest.
RUN python -c 'import os,re; assert re.fullmatch(r"aelev/translator-service:health-seed-sha256-[0-9a-f]{64}|.+@sha256:[0-9a-f]{64}", os.environ["AELEV_INFRASTRUCTURE_TRANSLATOR_HEALTH_SEED_IMAGE"]), "PINNED_HEALTH_SEED_REQUIRED"'
COPY src/translator_service/api.py src/translator_service/api_server.py src/translator_service/local_health.py src/translator_service/health_heartbeat.py /opt/aelev/src/translator_service/

# Owner background code and its opaque-port dependencies retain the seed stack.
FROM ${AELEV_INFRASTRUCTURE_TRANSLATOR_HEALTH_SEED_IMAGE} AS translator-background-update
ARG AELEV_INFRASTRUCTURE_TRANSLATOR_HEALTH_SEED_IMAGE
RUN python -c 'import os,re; assert re.fullmatch(r"aelev/translator-service:health-seed-sha256-[0-9a-f]{64}|.+@sha256:[0-9a-f]{64}", os.environ["AELEV_INFRASTRUCTURE_TRANSLATOR_HEALTH_SEED_IMAGE"]), "PINNED_HEALTH_SEED_REQUIRED"'
ENV LD_LIBRARY_PATH=/opt/aelev/sqlite
COPY --from=sqlite-builder /sqlite /opt/aelev/sqlite
COPY src/translator_service/runtime.py src/translator_service/local_health.py src/translator_service/health_heartbeat.py src/translator_service/provider_transport.py src/translator_service/provider_identity.py src/translator_service/provider_healthcheck.py src/translator_service/presence.py src/translator_service/retry.py /opt/aelev/src/translator_service/
RUN python -S -c 'import sqlite3; assert sqlite3.sqlite_version == "3.53.4"'

# Publisher migration does not replace its Python, SDK, OS or application code.
FROM ${AELEV_INFRASTRUCTURE_TRANSLATOR_HEALTH_SEED_IMAGE} AS translator-status-sqlite-update
ARG AELEV_INFRASTRUCTURE_TRANSLATOR_HEALTH_SEED_IMAGE
RUN python -c 'import os,re; assert re.fullmatch(r"aelev/translator-service:health-seed-sha256-[0-9a-f]{64}|.+@sha256:[0-9a-f]{64}", os.environ["AELEV_INFRASTRUCTURE_TRANSLATOR_HEALTH_SEED_IMAGE"]), "PINNED_HEALTH_SEED_REQUIRED"'
ENV LD_LIBRARY_PATH=/opt/aelev/sqlite
COPY --from=sqlite-builder /sqlite /opt/aelev/sqlite
RUN python -S -c 'import sqlite3; assert sqlite3.sqlite_version == "3.53.4"'

# Independently admitted compute image; the provider remains a separate product.
FROM translator-core-runtime AS translator-worker-runtime
LABEL org.opencontainers.image.source="https://github.com/sapogov1978/AELEV-platform-infrastructure" \
      io.aelev.translator.role="opaque-provider-worker"
USER 10001:10001
CMD ["python", "-m", "translator_service.runtime", "worker"]

# Backward-compatible default target remains the provider-neutral Translator core.
FROM translator-core-runtime AS translator-runtime
USER 10001:10001
