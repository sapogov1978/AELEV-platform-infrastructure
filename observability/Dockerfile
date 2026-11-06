FROM python:3.13.7-alpine3.22@sha256:9ba6d8cbebf0fb6546ae71f2a1c14f6ffd2fdab83af7fa5669734ef30ad48844 AS checks
RUN pip install --no-cache-dir poetry==2.2.1
WORKDIR /work
COPY pyproject.toml poetry.lock ./
RUN python -m venv /opt/venv && VIRTUAL_ENV=/opt/venv PATH=/opt/venv/bin:$PATH poetry install --with dev --no-root --no-interaction
RUN apk add --no-cache docker-cli docker-cli-compose openssl
ENV PATH=/opt/venv/bin:$PATH PYTHONDONTWRITEBYTECODE=1
COPY compose.yml .env.example prepare.py ./
COPY delivery ./delivery
COPY otel-collector ./otel-collector
COPY prometheus ./prometheus
COPY blackbox-exporter ./blackbox-exporter
COPY loki ./loki
COPY tempo ./tempo
COPY grafana ./grafana
COPY --from=tests / /work/test/
