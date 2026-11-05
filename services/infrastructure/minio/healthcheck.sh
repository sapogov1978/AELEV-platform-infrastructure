#!/bin/sh
set -eu

root_user_file=/run/secrets/minio-root-user
root_password_file=/run/secrets/minio-root-password
ca_file=/certs/CAs/aelev-minio-sites.crt
healthcheck_config=/tmp/minio-healthcheck

for required_file in "$root_user_file" "$root_password_file" "$ca_file"; do
  if [ ! -r "$required_file" ] || [ ! -s "$required_file" ]; then
    echo "required MinIO healthcheck file is missing, empty, or unreadable: $required_file" >&2
    exit 1
  fi
done

mkdir -p "$healthcheck_config/certs/CAs"
cp "$ca_file" "$healthcheck_config/certs/CAs/aelev-minio-sites.crt"
MC_CONFIG_DIR="$healthcheck_config" mc alias set healthcheck \
  https://localhost:9000 \
  "$(cat "$root_user_file")" \
  "$(cat "$root_password_file")" >/dev/null
MC_CONFIG_DIR="$healthcheck_config" exec mc ready healthcheck
