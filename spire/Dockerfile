ARG OWNER_SENDER_IMAGE=ghcr.io/sapogov1978/aelev-platform-infrastructure-observability-sender@sha256:162f07e6fd043eb3d9e04e7c415bee07771321a6f7f3864fcba0ce1f80dd941e
FROM python:3.13.7-slim-bookworm@sha256:adafcc17694d715c905b4c7bebd96907a1fd5cf183395f0ebc4d3428bd22d92d AS artifacts
ARG TARGETARCH
WORKDIR /work
COPY artifacts.lock.json build_artifacts.py ./
RUN python build_artifacts.py --architecture "${TARGETARCH}" --lock artifacts.lock.json --output /out

FROM python:3.13.7-slim-bookworm@sha256:adafcc17694d715c905b4c7bebd96907a1fd5cf183395f0ebc4d3428bd22d92d AS dependencies
ENV POETRY_VERSION=2.2.1 POETRY_NO_INTERACTION=1 POETRY_VIRTUALENVS_IN_PROJECT=1
WORKDIR /opt/aelev
COPY pyproject.toml poetry.lock ./
COPY vendor ./vendor
RUN pip install "poetry==${POETRY_VERSION}" && poetry install --only main --no-root --no-ansi

FROM dependencies AS check-dependencies
RUN poetry install --with dev --no-root --no-ansi

FROM python:3.13.7-slim-bookworm@sha256:adafcc17694d715c905b4c7bebd96907a1fd5cf183395f0ebc4d3428bd22d92d AS sqlite
RUN apt-get update && apt-get install --yes --no-install-recommends gcc libc6-dev && rm -rf /var/lib/apt/lists/*
WORKDIR /work
COPY artifacts.lock.json build_sqlite.py ./
RUN python build_sqlite.py --lock artifacts.lock.json --output /out

FROM python:3.13.7-slim-bookworm@sha256:adafcc17694d715c905b4c7bebd96907a1fd5cf183395f0ebc4d3428bd22d92d AS runtime
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    VIRTUAL_ENV=/opt/aelev/.venv PATH=/opt/aelev/.venv/bin:$PATH PYTHONPATH=/opt/aelev/src \
    LD_LIBRARY_PATH=/usr/local/lib
RUN apt-get update && apt-get upgrade --yes && rm -rf /var/lib/apt/lists/* \
    && groupadd --system --gid 10001 aelev \
    && useradd --system --uid 10001 --gid aelev --home-dir /nonexistent aelev
WORKDIR /opt/aelev
COPY --from=artifacts /out/ /opt/spire/bin/
COPY --from=dependencies /opt/aelev/.venv ./.venv
COPY --from=dependencies /opt/aelev/.venv/lib/python3.13/site-packages/aelev_platform_ui_client/health.py ./publisher-health.py
RUN install -d --owner=10001 --group=10001 /var/lib/spire /run/spire /run/svid /run/export /run/observation /var/lib/observation /var/lib/aelev-status \
    && python -m pip uninstall --yes pip \
    && /usr/local/bin/python -m pip uninstall --yes pip \
    && rm -rf /usr/local/lib/python3.13/ensurepip/_bundled
COPY --from=sqlite /out/libsqlite3.so.0 /usr/local/lib/libsqlite3.so.0
RUN ldconfig && python -c "import sqlite3; assert sqlite3.sqlite_version_info == (3, 53, 4), sqlite3.sqlite_version"
COPY artifacts.lock.json ./
COPY licenses/SPIRE-LICENSE-1.15.4 /licenses/spire/LICENSE
COPY src ./src
USER 10001:10001
ENTRYPOINT ["python", "-m", "aelev_spire"]

FROM docker:28.5.1-cli@sha256:9190b0613792e658a7783cf14b2d5ace5941bb68ede7276922ea36ee457d76ad AS docker-client
FROM runtime AS operator-installer
LABEL io.aelev.spire.bootstrap-export="1"
# Operator only. Runtime services use the separate runtime image without a socket.
COPY --from=docker-client /usr/local/bin/docker /usr/local/bin/docker
COPY --from=docker-client /usr/local/libexec/docker/cli-plugins/docker-compose /usr/local/libexec/docker/cli-plugins/docker-compose
COPY compose.yml compose.agent.yml compose.observation.yml /opt/aelev/
ENTRYPOINT ["python", "-m", "aelev_spire.installation"]

FROM runtime AS certificate-delivery
LABEL io.aelev.certificate-delivery="1"
COPY deployment/certificate-delivery.py /opt/aelev/certificate-delivery.py
ENTRYPOINT ["python", "/opt/aelev/certificate-delivery.py"]

FROM runtime AS checks
USER root
COPY --from=check-dependencies /opt/aelev/.venv ./.venv
COPY test ./test
COPY pyproject.toml build_artifacts.py build_sqlite.py ./
COPY compose.yml compose.agent.yml compose.observation.yml ./
USER 10001:10001
ENTRYPOINT ["python", "-m", "pytest"]
CMD ["-p", "no:cacheprovider"]

FROM python:3.13.7-alpine3.22@sha256:9ba6d8cbebf0fb6546ae71f2a1c14f6ffd2fdab83af7fa5669734ef30ad48844 AS sqlite-musl
RUN apk add --no-cache gcc musl-dev
WORKDIR /work
COPY artifacts.lock.json build_sqlite.py ./
RUN python build_sqlite.py --lock artifacts.lock.json --output /out

FROM ${OWNER_SENDER_IMAGE} AS telemetry-sender
USER root
COPY --from=sqlite-musl /out/libsqlite3.so.0 /usr/local/lib/libsqlite3.so.0
ENV LD_LIBRARY_PATH=/usr/local/lib
RUN python -S -c "import sqlite3; assert sqlite3.sqlite_version_info == (3, 53, 4)"
USER 10001:10001
