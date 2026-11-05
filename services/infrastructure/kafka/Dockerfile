# Runtime and build stage keep the exact already selected Kafka/Alpine release.
ARG KAFKA_BASE_IMAGE=apache/kafka:3.9.1@sha256:4ceccc577f03f51f6af8dbfda55194d0d892f4fa7913ffbded567ce3895622ed
FROM ${KAFKA_BASE_IMAGE} AS health-build
USER root
RUN apk add --no-cache gcc musl-dev
WORKDIR /work
COPY health.c ./
RUN cc -std=c11 -Wall -Wextra -Werror -Os -static -s health.c -o kafka-health

FROM ${KAFKA_BASE_IMAGE} AS runtime
COPY --from=health-build --chmod=0555 /work/kafka-health /opt/aelev/kafka-health
USER appuser
