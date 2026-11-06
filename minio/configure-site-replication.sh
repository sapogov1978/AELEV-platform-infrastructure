#!/bin/sh
set -eu

: "${AELEV_INFRASTRUCTURE_MINIO_SITE_A_ENDPOINT:?set the site A endpoint}"
: "${AELEV_INFRASTRUCTURE_MINIO_SITE_B_ENDPOINT:?set the site B endpoint}"
: "${MC_CONFIG_DIR:?set the temporary mc configuration directory}"

root_user_file=/run/secrets/minio-root-user
root_password_file=/run/secrets/minio-root-password
ca_file=/run/secrets/minio-sites-ca.crt

for secret_file in "$root_user_file" "$root_password_file" "$ca_file"; do
  if [ ! -r "$secret_file" ] || [ ! -s "$secret_file" ]; then
    echo "required MinIO secret file is missing, empty, or unreadable: $secret_file" >&2
    exit 1
  fi
done

case "$AELEV_INFRASTRUCTURE_MINIO_SITE_A_ENDPOINT" in
  https://*) ;;
  *) echo "site A endpoint must use HTTPS" >&2; exit 1 ;;
esac
case "$AELEV_INFRASTRUCTURE_MINIO_SITE_B_ENDPOINT" in
  https://*) ;;
  *) echo "site B endpoint must use HTTPS" >&2; exit 1 ;;
esac
if [ "$AELEV_INFRASTRUCTURE_MINIO_SITE_A_ENDPOINT" = "$AELEV_INFRASTRUCTURE_MINIO_SITE_B_ENDPOINT" ]; then
  echo "site replication requires two different site endpoints" >&2
  exit 1
fi

root_user=$(cat "$root_user_file")
root_password=$(cat "$root_password_file")
mkdir -p "$MC_CONFIG_DIR/certs/CAs"
cp "$ca_file" "$MC_CONFIG_DIR/certs/CAs/aelev-minio-sites-ca.crt"

mc alias set site-a "$AELEV_INFRASTRUCTURE_MINIO_SITE_A_ENDPOINT" "$root_user" "$root_password" >/dev/null
mc alias set site-b "$AELEV_INFRASTRUCTURE_MINIO_SITE_B_ENDPOINT" "$root_user" "$root_password" >/dev/null
unset root_user root_password

site_replication=$(mc --json admin replicate info site-a 2>&1 || true)
case "$site_replication" in
  *"$AELEV_INFRASTRUCTURE_MINIO_SITE_A_ENDPOINT"*)
    case "$site_replication" in
      *"$AELEV_INFRASTRUCTURE_MINIO_SITE_B_ENDPOINT"*) ;;
      *) echo "existing site replication does not contain the configured site B endpoint" >&2; exit 1 ;;
    esac
    ;;
  *'"enabled":false'*|*"not enabled"*|*"not configured"*)
    mc admin replicate add site-a site-b >/dev/null
    ;;
  *)
    echo "cannot identify the existing site-replication state: $site_replication" >&2
    exit 1
    ;;
esac

mc admin replicate info site-a
mc admin replicate status site-a
