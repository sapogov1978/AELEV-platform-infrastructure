ARG NODE_IMAGE
ARG NGINX_IMAGE
ARG PYTHON_IMAGE
ARG SQLITE_IMAGE=aelev/observability-sender:1.0.7@sha256:44e5063c37fd5268d221bf7886a66abb4a256a595190604470bbf1c5e5500739
FROM ${SQLITE_IMAGE} AS sqlite
FROM ${NODE_IMAGE} AS frontend-build
WORKDIR /work
COPY package.json package-lock.json ./
RUN npm ci --ignore-scripts
COPY tsconfig.json vite.config.ts index.html ./
COPY src ./src
COPY public ./public
RUN npm run build

FROM ${PYTHON_IMAGE} AS python-build
RUN pip install --no-cache-dir poetry==2.2.1
WORKDIR /work
COPY pyproject.toml poetry.lock ./
COPY vendor ./vendor
RUN python -m venv /opt/venv
ENV VIRTUAL_ENV=/opt/venv PATH=/opt/venv/bin:$PATH
RUN poetry install --only main --no-interaction --no-ansi

FROM python-build AS checks
COPY client ./client
RUN apk add --no-cache docker-cli docker-cli-compose openssl
RUN poetry install --with dev --no-interaction --no-ansi
COPY api.py prepare.py provision.py health.py publisher_authority.py .env.example nginx.conf compose.yml *.md ./
COPY status_publisher.py ./
COPY public ./public
COPY operator-ui-kit ./operator-ui-kit
COPY --from=tests / ./test/
COPY Dockerfile .dockerignore package.json package-lock.json ./
COPY src ./src

FROM ${PYTHON_IMAGE} AS api
COPY --from=python-build /opt/venv /opt/venv
COPY --from=python-build /opt/venv/lib/python3.13/site-packages/aelev_platform_ui_client/health.py /opt/platform-ui/publisher-health.py
ENV PATH=/opt/venv/bin:$PATH PYTHONDONTWRITEBYTECODE=1
RUN mkdir -p /var/lib/platform-ui && chown 101:101 /var/lib/platform-ui
COPY api.py provision.py health.py /opt/platform-ui/
COPY status_publisher.py /opt/platform-ui/status_publisher.py
RUN mkdir -p /var/lib/aelev-status && chown 101:101 /var/lib/aelev-status
USER 101:101
CMD ["python", "/opt/platform-ui/api.py", "/etc/platform-ui/api.json"]

FROM api AS publisher
USER 0:0
COPY --from=sqlite /usr/local/lib/libsqlite3.so.0 /usr/local/lib/libsqlite3.so.0
ENV LD_LIBRARY_PATH=/usr/local/lib
RUN python -S -c "import sqlite3; assert sqlite3.sqlite_version_info == (3, 53, 4), sqlite3.sqlite_version"
USER 101:101

FROM ${NGINX_IMAGE} AS ui
USER 0:0
RUN apk upgrade --no-cache
COPY --from=frontend-build /work/dist /usr/share/nginx/html
EXPOSE 8443
USER 101:101
