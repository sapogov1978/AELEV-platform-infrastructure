FROM python:3.13.7-slim-bookworm@sha256:adafcc17694d715c905b4c7bebd96907a1fd5cf183395f0ebc4d3428bd22d92d AS poetry-test

ENV POETRY_VERSION=2.2.1 \
    POETRY_NO_INTERACTION=1 \
    POETRY_VIRTUALENVS_IN_PROJECT=1

WORKDIR /opt/aelev-egress
RUN pip install --no-cache-dir "poetry==${POETRY_VERSION}"
COPY pyproject.toml poetry.lock ./
COPY vendor ./vendor
RUN poetry install --with dev --no-root --no-ansi

FROM poetry-test AS poetry-main
RUN poetry sync --only main --no-root --no-ansi

FROM python:3.13.7-slim-bookworm@sha256:adafcc17694d715c905b4c7bebd96907a1fd5cf183395f0ebc4d3428bd22d92d AS swagger-assets
ADD --checksum=sha256:050bc415ee7048dcd881682678f720264e7da5e373f7461d7c58c755305255f7 https://raw.githubusercontent.com/swagger-api/swagger-ui/v5.33.1/dist/swagger-ui-bundle.js /assets/swagger-ui-bundle.js
ADD --checksum=sha256:1ac324f7dcd27e4b9386b4bd6421271ec147e922a22c05ba24b11515e9aa6321 https://raw.githubusercontent.com/swagger-api/swagger-ui/v5.33.1/dist/swagger-ui.css /assets/swagger-ui.css
ADD --checksum=sha256:cfc7749b96f63bd31c3c42b5c471bf756814053e847c10f3eb003417bc523d30 https://raw.githubusercontent.com/swagger-api/swagger-ui/v5.33.1/LICENSE /assets/LICENSE
RUN chmod 644 /assets/*

FROM python:3.13.7-slim-bookworm@sha256:adafcc17694d715c905b4c7bebd96907a1fd5cf183395f0ebc4d3428bd22d92d AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN groupadd --system --gid 10001 aelev \
    && useradd --system --uid 10001 --gid aelev --home-dir /nonexistent aelev

WORKDIR /opt/aelev-egress

FROM base AS runtime

ENV VIRTUAL_ENV=/opt/aelev-egress/.venv PATH=/opt/aelev-egress/.venv/bin:$PATH
COPY --from=poetry-main /opt/aelev-egress/.venv ./.venv
COPY gateway.py telemetry.py api_contract.py documentation.py authorization.py health.py ./
COPY --from=swagger-assets /assets ./swagger-assets

USER 10001:10001
EXPOSE 8080 8081
CMD ["python", "gateway.py"]

FROM runtime AS status
USER root
RUN install -d --owner=10001 --group=10001 /var/lib/aelev-status
COPY status_publisher.py ./
COPY --from=poetry-main /opt/aelev-egress/.venv/lib/python3.13/site-packages/aelev_platform_ui_client/health.py ./publisher-health.py
USER 10001:10001
ENTRYPOINT ["python", "status_publisher.py"]
CMD ["run"]

FROM base AS test

ENV HOME=/tmp/aelev-home \
    XDG_CACHE_HOME=/tmp/aelev-cache \
    VIRTUAL_ENV=/opt/aelev-egress/.venv \
    PATH=/opt/aelev-egress/.venv/bin:$PATH
COPY --from=poetry-test /opt/aelev-egress/.venv ./.venv
COPY pyproject.toml ./pyproject.toml
COPY Dockerfile ./Dockerfile
COPY gateway.py telemetry.py api_contract.py documentation.py authorization.py provision.py prepare.py renewal.py renewal_schedule.py health.py status_publisher.py ./
COPY --from=swagger-assets /assets ./swagger-assets
COPY test ./test/
COPY compose.yml .env.example ./
COPY observability ./observability
RUN install -d --owner=10001 --group=10001 \
    /tmp/aelev-home /tmp/aelev-cache /opt/aelev-egress/.pytest_cache \
    /opt/aelev-egress/.ruff_cache

USER 10001:10001
CMD ["python", "-m", "pytest", "test"]
