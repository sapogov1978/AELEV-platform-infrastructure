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

FROM postgres:17.6-alpine@sha256:ef257d85f76e48da1c64832459b59fcaba1a4dac97bf5d7450c77753542eee94 AS runtime-base
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

# One-shot node operation and remote executor use only the standard library.
FROM python-runtime AS fencing-node
RUN apk add --no-cache musl=1.2.5-r13 musl-utils=1.2.5-r13 libuuid=2.41.6-r1 zlib=1.4-r0 \
        busybox=1.37.0-r20 busybox-binsh=1.37.0-r20 ssl_client=1.37.0-r20 \
        xz-libs=5.8.4-r0 openssl=3.5.9-r0 \
    && rm -rf /usr/local/lib/python3.13/site-packages/pip \
        /usr/local/lib/python3.13/site-packages/pip-*.dist-info \
        /usr/local/lib/python3.13/ensurepip \
        /usr/local/bin/pip /usr/local/bin/pip3 /usr/local/bin/pip3.13 \
    && mkdir -m 700 /var/lib/aelev-fencing
COPY src/aelev_postgresql/__init__.py src/aelev_postgresql/configuration.py \
    src/aelev_postgresql/fencing.py src/aelev_postgresql/fencing_intent.py \
    src/aelev_postgresql/fencing_return.py src/aelev_postgresql/fencing_node.py \
    src/aelev_postgresql/fencing_executor.py src/aelev_postgresql/fencing_probe.py /opt/aelev/aelev_postgresql/
ENV PYTHONPATH=/opt/aelev PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
ENTRYPOINT ["python", "-m", "aelev_postgresql.fencing_executor"]
CMD ["serve"]

# Candidate opt-in transport, separate from the default SQL/local-fencer image.
FROM fencing-node AS fencing-ssh
RUN apk add --no-cache openssh-client=10.0_p1-r11
COPY src/aelev_postgresql/fencing_ssh.py /opt/aelev/aelev_postgresql/

FROM runtime-base AS runtime
