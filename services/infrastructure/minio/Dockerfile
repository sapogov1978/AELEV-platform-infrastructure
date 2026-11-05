# syntax=docker/dockerfile:1.7

FROM golang:1.24.2-alpine3.21@sha256:7772cb5322baa875edd74705556d08f0eeca7b9c4b5367754ce3f2f00041ccee AS vendor-build

ARG MINIO_RELEASE=RELEASE.2025-04-22T22-12-26Z
ARG MINIO_COMMIT=0d7408fc9969caf07de6a8c3a84f9fbb10a6739e
ARG MC_RELEASE=RELEASE.2025-04-16T18-13-26Z
ARG MC_COMMIT=b00526b153a31b36767991a4f5ce2cced435ee8e

ENV CGO_ENABLED=0
ENV GOBIN=/out

RUN apk add --no-cache ca-certificates
RUN --mount=type=cache,target=/go/pkg/mod \
    --mount=type=cache,target=/root/.cache/go-build \
    go install -tags kqueue -trimpath -ldflags="-s -w \
      -X github.com/minio/minio/cmd.Version=2025-04-22T22:12:26Z \
      -X github.com/minio/minio/cmd.CopyrightYear=2025 \
      -X github.com/minio/minio/cmd.ReleaseTag=${MINIO_RELEASE} \
      -X github.com/minio/minio/cmd.CommitID=${MINIO_COMMIT} \
      -X github.com/minio/minio/cmd.ShortCommitID=0d7408fc9969" \
      "github.com/minio/minio@${MINIO_COMMIT}"
RUN --mount=type=cache,target=/go/pkg/mod \
    --mount=type=cache,target=/root/.cache/go-build \
    go install -tags kqueue -trimpath -ldflags="-s -w \
      -X github.com/minio/mc/cmd.Version=2025-04-16T18:13:26Z \
      -X github.com/minio/mc/cmd.CopyrightYear=2025 \
      -X github.com/minio/mc/cmd.ReleaseTag=${MC_RELEASE} \
      -X github.com/minio/mc/cmd.CommitID=${MC_COMMIT} \
      -X github.com/minio/mc/cmd.ShortCommitID=b00526b153a3" \
      "github.com/minio/mc@${MC_COMMIT}"
RUN --mount=type=cache,target=/go/pkg/mod \
    mkdir -p /out/licenses/minio /out/licenses/mc && \
    cp /go/pkg/mod/github.com/minio/minio@v0.0.0-20250422221226-0d7408fc9969/LICENSE /out/licenses/minio/LICENSE && \
    cp /go/pkg/mod/github.com/minio/minio@v0.0.0-20250422221226-0d7408fc9969/CREDITS /out/licenses/minio/CREDITS && \
    cp /go/pkg/mod/github.com/minio/mc@v0.0.0-20250416181326-b00526b153a3/LICENSE /out/licenses/mc/LICENSE && \
    test -x /out/minio && test -x /out/mc

FROM registry.access.redhat.com/ubi9/ubi-micro:9.5@sha256:839f16991579b023d4452eadd0efa925e438f8b73063afe4f75bdc6cf7a09b12 AS runtime

ARG MINIO_RELEASE=RELEASE.2025-04-22T22-12-26Z
ARG MINIO_COMMIT=0d7408fc9969caf07de6a8c3a84f9fbb10a6739e
ARG MC_RELEASE=RELEASE.2025-04-16T18-13-26Z
ARG MC_COMMIT=b00526b153a31b36767991a4f5ce2cced435ee8e

LABEL org.opencontainers.image.source="https://github.com/minio/minio" \
      org.opencontainers.image.version="${MINIO_RELEASE}" \
      org.opencontainers.image.revision="${MINIO_COMMIT}" \
      org.opencontainers.image.licenses="AGPL-3.0-or-later" \
      io.aelev.minio.mc.version="${MC_RELEASE}" \
      io.aelev.minio.mc.revision="${MC_COMMIT}"

ENV MINIO_ACCESS_KEY_FILE=access_key \
    MINIO_SECRET_KEY_FILE=secret_key \
    MINIO_ROOT_USER_FILE=access_key \
    MINIO_ROOT_PASSWORD_FILE=secret_key \
    MINIO_KMS_SECRET_KEY_FILE=kms_master_key \
    MINIO_CONFIG_ENV_FILE=config.env \
    MC_CONFIG_DIR=/tmp/.mc

COPY --from=vendor-build /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-certificates.crt
COPY --from=vendor-build --chmod=0555 /out/minio /usr/bin/minio
COPY --from=vendor-build --chmod=0555 /out/mc /usr/bin/mc
COPY --from=vendor-build /out/licenses /licenses
COPY --chmod=0555 healthcheck.sh /usr/bin/minio-healthcheck

EXPOSE 9000
VOLUME ["/data"]
ENTRYPOINT ["/usr/bin/minio"]
