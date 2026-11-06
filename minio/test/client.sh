#!/bin/sh
set -eu

: "${AELEV_MINIO_TEST_SITE_A_ENDPOINT:?set the test site A endpoint}"
: "${AELEV_MINIO_TEST_SITE_B_ENDPOINT:?set the test site B endpoint}"
: "${AELEV_MINIO_TEST_ACCESS_KEY:?set the test access key}"
: "${AELEV_MINIO_TEST_SECRET_KEY:?set the test secret key}"
: "${AELEV_MINIO_TEST_BUCKET:?set the test bucket}"
: "${AELEV_MINIO_TEST_OBJECT:?set the test object name}"
: "${MC_CONFIG_DIR:?set the temporary mc configuration directory}"

ca_file=/run/secrets/minio-sites-ca.crt
if [ ! -r "$ca_file" ] || [ ! -s "$ca_file" ]; then
  echo "required MinIO CA file is missing, empty, or unreadable: $ca_file" >&2
  exit 1
fi

mkdir -p "$MC_CONFIG_DIR/certs/CAs"
cp "$ca_file" "$MC_CONFIG_DIR/certs/CAs/aelev-minio-sites-ca.crt"
mc alias set site-a "$AELEV_MINIO_TEST_SITE_A_ENDPOINT" "$AELEV_MINIO_TEST_ACCESS_KEY" "$AELEV_MINIO_TEST_SECRET_KEY" >/dev/null
mc alias set site-b "$AELEV_MINIO_TEST_SITE_B_ENDPOINT" "$AELEV_MINIO_TEST_ACCESS_KEY" "$AELEV_MINIO_TEST_SECRET_KEY" >/dev/null

case "${1:-}" in
  upload)
    [ "$#" -eq 2 ] || {
      echo "usage: test-client upload <attempts>" >&2
      exit 2
    }
    attempt=1
    while [ "$attempt" -le "$2" ]; do
      if mc cp --limit-upload "$AELEV_MINIO_TEST_UPLOAD_LIMIT" --checksum SHA256 \
        /test/large.bin "site-a/$AELEV_MINIO_TEST_BUCKET/$AELEV_MINIO_TEST_OBJECT"; then
        exit 0
      fi
      echo "upload attempt $attempt failed; retrying through the site endpoint" >&2
      attempt=$((attempt + 1))
      sleep 1
    done
    echo "large-object upload did not complete after $2 attempts" >&2
    exit 1
    ;;
  hash)
    [ "$#" -eq 2 ] || {
      echo "usage: test-client hash <site-a|site-b>" >&2
      exit 2
    }
    mc cat "$2/$AELEV_MINIO_TEST_BUCKET/$AELEV_MINIO_TEST_OBJECT" | sha256sum | {
      read -r object_sha256 _
      printf '%s\n' "$object_sha256"
    }
    ;;
  wait-object)
    [ "$#" -eq 3 ] || {
      echo "usage: test-client wait-object <site-a|site-b> <attempts>" >&2
      exit 2
    }
    attempt=1
    while [ "$attempt" -le "$3" ]; do
      if mc stat "$2/$AELEV_MINIO_TEST_BUCKET/$AELEV_MINIO_TEST_OBJECT" >/dev/null 2>&1; then
        exit 0
      fi
      attempt=$((attempt + 1))
      sleep 2
    done
    echo "object did not become readable from $2 after $3 attempts" >&2
    exit 1
    ;;
  *)
    exec "$@"
    ;;
esac
