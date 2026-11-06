FROM python:3.13.7-slim-bookworm@sha256:adafcc17694d715c905b4c7bebd96907a1fd5cf183395f0ebc4d3428bd22d92d AS dependencies
ENV POETRY_VERSION=2.2.1 POETRY_NO_INTERACTION=1 POETRY_VIRTUALENVS_IN_PROJECT=1
WORKDIR /opt/download
RUN pip install "poetry==${POETRY_VERSION}"
COPY pyproject.toml poetry.lock ./
RUN poetry install --only main --no-root --no-ansi

FROM python:3.13.7-slim-bookworm@sha256:adafcc17694d715c905b4c7bebd96907a1fd5cf183395f0ebc4d3428bd22d92d
LABEL org.aelev.translator.experiment="index-translate-20261011"
COPY --from=dependencies /opt/download/.venv /opt/download/.venv
ENV HF_HOME=/downloads/hub-cache HF_XET_CACHE=/downloads/xet-cache \
    HF_HUB_DISABLE_IMPLICIT_TOKEN=1 HF_HUB_DISABLE_TELEMETRY=1 \
    HF_XET_HIGH_PERFORMANCE=1 HF_XET_NUM_CONCURRENT_RANGE_GETS=16
USER 10001:10001
ENTRYPOINT ["/opt/download/.venv/bin/hf", "download"]
