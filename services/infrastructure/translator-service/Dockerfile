ARG AELEV_INFRASTRUCTURE_TRANSLATOR_RUNTIME_SEED_IMAGE=scratch
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

FROM python-bookworm AS index-model-assets
# Byte-identical official mirror of the Hugging Face revision in translator.lock.json.
COPY translator.lock.json download-model.py /supply/
RUN --mount=type=cache,id=aelev-translator-index-weights,target=/var/cache/aelev-translator \
    python /supply/download-model.py --lock /supply/translator.lock.json \
    --destination /model --cache /var/cache/aelev-translator

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
COPY translator.lock.json ./translator.lock.json
COPY Dockerfile ./Dockerfile
COPY download-model.py ./download-model.py
COPY --from=swagger-assets /assets ./swagger-assets
CMD ["poetry", "run", "pytest", "-q"]

FROM python-bookworm AS status-runtime
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONPATH=/opt/aelev/src \
    VIRTUAL_ENV=/opt/aelev/.venv PATH=/opt/aelev/.venv/bin:$PATH \
    LD_LIBRARY_PATH=/opt/aelev/sqlite \
    AELEV_TRANSLATOR_LOCK_PATH=/opt/aelev/translator.lock.json
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
COPY translator.lock.json ./translator.lock.json
USER 10001:10001
RUN python -c 'import os; from pathlib import Path; from translator_service.index_translation_provider import read_translator_lock; read_translator_lock(Path(os.environ["AELEV_TRANSLATOR_LOCK_PATH"]))'
ENTRYPOINT ["python", "-m", "translator_service.status_publisher"]
CMD ["run"]

FROM python-bookworm AS index-runtime-builder
RUN apt-get update \
    && apt-get install --yes --no-install-recommends cmake g++ make \
    && rm -rf /var/lib/apt/lists/*
ADD --checksum=sha256:6b58785f0a82898f4c3442417ff962e2d4b231b1bee5d033902ad90b27901e14 https://codeload.github.com/ggml-org/llama.cpp/tar.gz/d81235049384534c167caea52b85a694f6103d14 /source.tar.gz
RUN mkdir /source \
    && tar -xzf /source.tar.gz --strip-components=1 -C /source \
    && cmake -S /source -B /source/build \
       -DCMAKE_BUILD_TYPE=Release -DGGML_NATIVE=OFF -DGGML_OPENMP=OFF \
       -DLLAMA_BUILD_IS_DEV=OFF -DLLAMA_BUILD_NUMBER=0 \
       -DLLAMA_BUILD_COMMIT=d81235049384534c167caea52b85a694f6103d14 \
       -DLLAMA_CURL=OFF -DLLAMA_BUILD_TESTS=OFF -DLLAMA_BUILD_EXAMPLES=OFF \
       -DLLAMA_BUILD_SERVER=ON -DLLAMA_BUILD_APP=OFF \
       -DLLAMA_BUILD_UI=OFF -DLLAMA_USE_PREBUILT_UI=OFF \
    && cmake --build /source/build --parallel 2 --target llama-server

FROM python-bookworm AS translator-base
LABEL io.aelev.compute.owner="translator-service" \
      io.aelev.compute.station="1"
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/opt/aelev/src \
    VIRTUAL_ENV=/opt/aelev/.venv \
    PATH=/opt/aelev/.venv/bin:$PATH \
    HOME=/tmp \
    XDG_CACHE_HOME=/tmp \
    AELEV_TRANSLATOR_MODEL_ROOT=/opt/aelev/model \
    AELEV_TRANSLATOR_LOCK_PATH=/opt/aelev/translator.lock.json \
    AELEV_TRANSLATOR_SOCKET_PATH=/run/aelev-translator/translator.sock \
    AELEV_TRANSLATOR_RUNTIME_PATH=/opt/llama/llama-server \
    LD_LIBRARY_PATH=/opt/aelev/sqlite:/opt/llama
COPY --from=sqlite-builder /sqlite /opt/aelev/sqlite
RUN python -S -c 'import sqlite3; assert sqlite3.sqlite_version == "3.53.4"'
RUN groupadd --system --gid 10001 aelev \
    && useradd --system --uid 10001 --gid aelev --home-dir /nonexistent aelev \
    && install -d --owner=10001 --group=10001 /opt/aelev/model /run/aelev-translator /var/lib/aelev-status
WORKDIR /opt/aelev
COPY --from=dependencies /opt/aelev/.venv ./.venv
RUN python -m pip uninstall --yes pip \
    && rm -rf /usr/local/lib/python3.13/ensurepip/_bundled
COPY --from=index-runtime-builder /source/build/bin/ /opt/llama/

# Health-only maintenance keeps the exact interpreter, dependencies and weights.
FROM ${AELEV_INFRASTRUCTURE_TRANSLATOR_HEALTH_SEED_IMAGE} AS translator-health-update
ARG AELEV_INFRASTRUCTURE_TRANSLATOR_HEALTH_SEED_IMAGE
# A local snapshot tag is admitted only after checking its exact image ID and
# all inherited layers before and after the build; it is not a registry digest.
RUN python -c 'import os,re; assert re.fullmatch(r"aelev/translator-service:health-seed-sha256-[0-9a-f]{64}|.+@sha256:[0-9a-f]{64}", os.environ["AELEV_INFRASTRUCTURE_TRANSLATOR_HEALTH_SEED_IMAGE"]), "PINNED_HEALTH_SEED_REQUIRED"'
COPY src/translator_service/api.py src/translator_service/api_server.py src/translator_service/index_translation_provider.py src/translator_service/local_health.py src/translator_service/health_heartbeat.py /opt/aelev/src/translator_service/

# Model lifecycle maintenance keeps the exact weights, OS and dependencies.
FROM ${AELEV_INFRASTRUCTURE_TRANSLATOR_HEALTH_SEED_IMAGE} AS translator-model-lifecycle-update
ARG AELEV_INFRASTRUCTURE_TRANSLATOR_HEALTH_SEED_IMAGE
RUN python -c 'import os,re; assert re.fullmatch(r"aelev/translator-service:health-seed-sha256-[0-9a-f]{64}|.+@sha256:[0-9a-f]{64}", os.environ["AELEV_INFRASTRUCTURE_TRANSLATOR_HEALTH_SEED_IMAGE"]), "PINNED_HEALTH_SEED_REQUIRED"'
COPY src/translator_service/index_translation_provider.py /opt/aelev/src/translator_service/

# Maintenance changes only this owner's progress markers and SQLite ABI.
FROM ${AELEV_INFRASTRUCTURE_TRANSLATOR_HEALTH_SEED_IMAGE} AS translator-background-update
ARG AELEV_INFRASTRUCTURE_TRANSLATOR_HEALTH_SEED_IMAGE
RUN python -c 'import os,re; assert re.fullmatch(r"aelev/translator-service:health-seed-sha256-[0-9a-f]{64}|.+@sha256:[0-9a-f]{64}", os.environ["AELEV_INFRASTRUCTURE_TRANSLATOR_HEALTH_SEED_IMAGE"]), "PINNED_HEALTH_SEED_REQUIRED"'
ENV LD_LIBRARY_PATH=/opt/aelev/sqlite:/opt/llama
COPY --from=sqlite-builder /sqlite /opt/aelev/sqlite
COPY src/translator_service/runtime.py src/translator_service/local_health.py src/translator_service/health_heartbeat.py /opt/aelev/src/translator_service/
RUN python -S -c 'import sqlite3; assert sqlite3.sqlite_version == "3.53.4"'

# Publisher migration does not replace its Python, SDK, OS or application code.
FROM ${AELEV_INFRASTRUCTURE_TRANSLATOR_HEALTH_SEED_IMAGE} AS translator-status-sqlite-update
ARG AELEV_INFRASTRUCTURE_TRANSLATOR_HEALTH_SEED_IMAGE
RUN python -c 'import os,re; assert re.fullmatch(r"aelev/translator-service:health-seed-sha256-[0-9a-f]{64}|.+@sha256:[0-9a-f]{64}", os.environ["AELEV_INFRASTRUCTURE_TRANSLATOR_HEALTH_SEED_IMAGE"]), "PINNED_HEALTH_SEED_REQUIRED"'
ENV LD_LIBRARY_PATH=/opt/aelev/sqlite
COPY --from=sqlite-builder /sqlite /opt/aelev/sqlite
RUN python -S -c 'import sqlite3; assert sqlite3.sqlite_version == "3.53.4"'

# Maintenance target: reuse only this owner's digest-pinned approved runtime.
# The default fresh-build target below never depends on a prior release.
FROM ${AELEV_INFRASTRUCTURE_TRANSLATOR_RUNTIME_SEED_IMAGE} AS translator-runtime-update
ARG AELEV_INFRASTRUCTURE_TRANSLATOR_RUNTIME_SEED_IMAGE
USER 0:0
WORKDIR /opt/aelev
RUN python -c 'import os,re; assert re.fullmatch(r".+@sha256:[0-9a-f]{64}", os.environ["AELEV_INFRASTRUCTURE_TRANSLATOR_RUNTIME_SEED_IMAGE"]), "PINNED_RUNTIME_SEED_REQUIRED"'
RUN python -c 'import shutil; from pathlib import Path; root=Path("/opt/aelev"); [shutil.rmtree(root/name) for name in (".venv", "src", "contracts", "alembic", "swagger-assets") if (root/name).exists()]'
COPY --from=dependencies /opt/aelev/.venv ./.venv
COPY translator.lock.json ./translator.lock.json
COPY --from=swagger-assets /assets ./swagger-assets
COPY src ./src
COPY contracts ./contracts
COPY alembic.ini ./alembic.ini
COPY alembic ./alembic
RUN python -m translator_service.index_translation_provider verify-model
USER 10001:10001
CMD ["python", "-m", "translator_service.index_translation_provider", "serve"]

FROM translator-base AS translator-runtime
COPY --from=index-model-assets --chown=10001:10001 --chmod=644 /model/Index-Translate-9B.Q4_K_M.gguf /opt/aelev/model/Index-Translate-9B.Q4_K_M.gguf
COPY translator.lock.json ./translator.lock.json
COPY --from=swagger-assets /assets ./swagger-assets
COPY src ./src
COPY contracts ./contracts
COPY alembic.ini ./alembic.ini
COPY alembic ./alembic
RUN python -m translator_service.index_translation_provider verify-model
USER 10001:10001
CMD ["python", "-m", "translator_service.index_translation_provider", "serve"]
