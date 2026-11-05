FROM python:3.13.7-alpine3.22@sha256:9ba6d8cbebf0fb6546ae71f2a1c14f6ffd2fdab83af7fa5669734ef30ad48844 AS python-runtime
FROM python-runtime AS builder
RUN pip install --no-cache-dir poetry==2.2.1
WORKDIR /build
COPY pyproject.toml poetry.lock ./
COPY vendor ./vendor
RUN python -m venv /opt/venv && VIRTUAL_ENV=/opt/venv PATH=/opt/venv/bin:$PATH poetry install --only main --no-root --no-interaction

FROM builder AS checks
RUN apk add --no-cache docker-cli docker-cli-compose openssl
RUN VIRTUAL_ENV=/opt/venv PATH=/opt/venv/bin:$PATH poetry install --with dev --no-root --no-interaction
ENV VIRTUAL_ENV=/opt/venv PATH=/opt/venv/bin:$PATH PYTHONPATH=/work/src:/work
WORKDIR /work
COPY src ./src
COPY vendor ./vendor
COPY prepare-tls.py renewal.py status_publisher.py entrypoint.sh ./
COPY kubernetes ./kubernetes
COPY compose*.yml .env.example .dockerignore Dockerfile pyproject.toml poetry.lock README.md SPEC.md TASKS.md USER_MANUAL.md FENCING.md CHANGELOG.md ./
COPY --from=tests . ./test

FROM postgres:17.6-alpine@sha256:ef257d85f76e48da1c64832459b59fcaba1a4dac97bf5d7450c77753542eee94 AS runtime
RUN apk add --no-cache libffi libbz2 libexpat libuuid xz-libs ncurses-libs readline sqlite-libs openssl su-exec \
    && mkdir -p /opt/aelev /run/aelev && chown postgres:postgres /run/aelev
COPY --from=python-runtime /usr/local/ /usr/local/
COPY --from=builder /opt/venv /opt/venv
COPY src /opt/aelev
COPY status_publisher.py /opt/aelev/status_publisher.py
COPY renewal.py /opt/aelev/sql-client-renewal.py
COPY vendor/aelev-renewal-delivery-1.0.0.zip /opt/aelev/vendor/aelev-renewal-delivery-1.0.0.zip
COPY --from=builder /opt/venv/lib/python3.13/site-packages/aelev_platform_ui_client/health.py /opt/aelev/publisher-health.py
RUN mkdir -p /var/lib/aelev-status && chown 101:101 /var/lib/aelev-status
COPY entrypoint.sh /usr/local/bin/aelev-postgresql-entrypoint
ENV PATH=/opt/venv/bin:$PATH PYTHONPATH=/opt/aelev PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
ENTRYPOINT ["/bin/sh", "/usr/local/bin/aelev-postgresql-entrypoint"]
CMD ["start"]
