#!/bin/sh
set -eu

file_provision_error() {
  printf '%s\n' "$1" >&2
  exit 1
}

read_file_credential() {
  credential_path=$1
  case "$credential_path" in
    /*) ;;
    *) file_provision_error MINIO_PROVISION_CREDENTIAL_FILE_INVALID ;;
  esac
  if [ ! -f "$credential_path" ] || [ -L "$credential_path" ] || [ ! -r "$credential_path" ] || [ ! -s "$credential_path" ]; then
    file_provision_error MINIO_PROVISION_CREDENTIAL_FILE_INVALID
  fi
  credential_size=$(LC_ALL=C wc -c < "$credential_path" 2>/dev/null) || file_provision_error MINIO_PROVISION_CREDENTIAL_FILE_INVALID
  if [ "$credential_size" -gt 4096 ]; then
    file_provision_error MINIO_PROVISION_CREDENTIAL_LINE_INVALID
  fi
  # Check bytes before shell command substitution could discard a NUL or final LF.
  credential_lines=$(LC_ALL=C tr -cd '\n' < "$credential_path" 2>/dev/null | wc -c) || file_provision_error MINIO_PROVISION_CREDENTIAL_FILE_INVALID
  credential_printable=$(LC_ALL=C tr -cd '\040-\176' < "$credential_path" 2>/dev/null | wc -c) || file_provision_error MINIO_PROVISION_CREDENTIAL_FILE_INVALID
  if [ "$credential_lines" -gt 1 ] || [ "$credential_size" -ne "$((credential_printable + credential_lines))" ]; then
    file_provision_error MINIO_PROVISION_CREDENTIAL_LINE_INVALID
  fi
  credential_value=$(cat "$credential_path" 2>/dev/null) || file_provision_error MINIO_PROVISION_CREDENTIAL_FILE_INVALID
  credential_newline='
'
  case "$credential_value" in
    *"$credential_newline"*) file_provision_error MINIO_PROVISION_CREDENTIAL_LINE_INVALID ;;
  esac
  credential_decoded_size=$(printf '%s' "$credential_value" | wc -c) || file_provision_error MINIO_PROVISION_CREDENTIAL_FILE_INVALID
  if [ "$credential_decoded_size" -eq 0 ] || [ "$credential_decoded_size" -gt 4095 ] || [ "$credential_decoded_size" -ne "$credential_printable" ]; then
    file_provision_error MINIO_PROVISION_CREDENTIAL_LINE_INVALID
  fi
}

file_mc() {
  if ! mc "$@" > "$file_diagnostic" 2>&1; then
    file_provision_error MINIO_PROVISION_MC_FAILED
  fi
}

provision_bucket_from_files() {
  set +x
  : "${AELEV_INFRASTRUCTURE_MINIO_ENDPOINT:?set the MinIO endpoint}"
  : "${AELEV_INFRASTRUCTURE_MINIO_PROVISION_BUCKET:?set the bucket to provision}"
  : "${AELEV_INFRASTRUCTURE_MINIO_PROVISION_POLICY:?set the provisioned policy name}"
  : "${MC_CONFIG_DIR:?set the temporary mc configuration directory}"
  : "${AELEV_INFRASTRUCTURE_MINIO_PROVISION_ACCESS_KEY_FILE:?set both provisioned credential files}"
  : "${AELEV_INFRASTRUCTURE_MINIO_PROVISION_SECRET_KEY_FILE:?set both provisioned credential files}"
  if [ -n "${AELEV_INFRASTRUCTURE_MINIO_PROVISION_ACCESS_KEY:-}" ] || [ -n "${AELEV_INFRASTRUCTURE_MINIO_PROVISION_SECRET_KEY:-}" ]; then
    file_provision_error MINIO_PROVISION_CREDENTIAL_MODE_AMBIGUOUS
  fi
  # Do not assign file bytes to inherited/exported inline credential variables.
  unset AELEV_INFRASTRUCTURE_MINIO_PROVISION_ACCESS_KEY AELEV_INFRASTRUCTURE_MINIO_PROVISION_SECRET_KEY
  unset credential_value file_root_user file_root_password file_access_key file_secret_key
  read_file_credential /run/secrets/minio-root-user
  file_root_user=$credential_value
  read_file_credential /run/secrets/minio-root-password
  file_root_password=$credential_value
  read_file_credential "$AELEV_INFRASTRUCTURE_MINIO_PROVISION_ACCESS_KEY_FILE"
  file_access_key=$credential_value
  read_file_credential "$AELEV_INFRASTRUCTURE_MINIO_PROVISION_SECRET_KEY_FILE"
  file_secret_key=$credential_value
  unset credential_value
  file_ca=/run/secrets/minio-ca.crt
  if [ ! -f "$file_ca" ] || [ ! -r "$file_ca" ] || [ ! -s "$file_ca" ] || [ -L "$MC_CONFIG_DIR" ]; then
    file_provision_error MINIO_PROVISION_PRIVATE_CONFIGURATION_INVALID
  fi
  umask 077
  mkdir -p "$MC_CONFIG_DIR/certs/CAs" || file_provision_error MINIO_PROVISION_PRIVATE_CONFIGURATION_INVALID
  chmod 0700 "$MC_CONFIG_DIR" "$MC_CONFIG_DIR/certs" "$MC_CONFIG_DIR/certs/CAs" 2>/dev/null || file_provision_error MINIO_PROVISION_PRIVATE_CONFIGURATION_INVALID
  cp "$file_ca" "$MC_CONFIG_DIR/certs/CAs/aelev-minio-ca.crt" 2>/dev/null || file_provision_error MINIO_PROVISION_PRIVATE_CONFIGURATION_INVALID
  file_diagnostic=$MC_CONFIG_DIR/provision-file.log
  if [ -L "$file_diagnostic" ]; then
    file_provision_error MINIO_PROVISION_PRIVATE_CONFIGURATION_INVALID
  fi
  : > "$file_diagnostic"
  chmod 0600 "$file_diagnostic" 2>/dev/null || file_provision_error MINIO_PROVISION_PRIVATE_CONFIGURATION_INVALID
  # Pinned mc reads two lines from non-TTY stdin; neither password enters argv/env.
  if ! printf '%s\n%s\n' "$file_root_user" "$file_root_password" | mc alias set infrastructure "$AELEV_INFRASTRUCTURE_MINIO_ENDPOINT" > "$file_diagnostic" 2>&1; then
    file_provision_error MINIO_PROVISION_ALIAS_FAILED
  fi
  unset file_root_user file_root_password
  file_mc mb --ignore-existing "infrastructure/$AELEV_INFRASTRUCTURE_MINIO_PROVISION_BUCKET"
  file_mc version enable "infrastructure/$AELEV_INFRASTRUCTURE_MINIO_PROVISION_BUCKET"
  if ! printf '%s\n%s\n' "$file_access_key" "$file_secret_key" | mc admin user add infrastructure > "$file_diagnostic" 2>&1; then
    file_provision_error MINIO_PROVISION_USER_FAILED
  fi
  unset file_secret_key
  file_policy=$MC_CONFIG_DIR/policy.json
  cat > "$file_policy" <<EOF
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": ["s3:GetBucketLocation", "s3:ListBucket", "s3:ListBucketVersions"],
      "Resource": ["arn:aws:s3:::$AELEV_INFRASTRUCTURE_MINIO_PROVISION_BUCKET"]
    },
    {
      "Effect": "Allow",
      "Action": ["s3:GetObject", "s3:PutObject", "s3:DeleteObject", "s3:DeleteObjectVersion"],
      "Resource": ["arn:aws:s3:::$AELEV_INFRASTRUCTURE_MINIO_PROVISION_BUCKET/*"]
    }
  ]
}
EOF
  file_mc admin policy create infrastructure "$AELEV_INFRASTRUCTURE_MINIO_PROVISION_POLICY" "$file_policy"
  # Access key is the public username; passwords are never command arguments.
  file_mc admin policy attach infrastructure "$AELEV_INFRASTRUCTURE_MINIO_PROVISION_POLICY" --user "$file_access_key"
  unset file_access_key
}

if [ "${AELEV_INFRASTRUCTURE_MINIO_PROVISION_ACCESS_KEY_FILE+x}" = x ] || [ "${AELEV_INFRASTRUCTURE_MINIO_PROVISION_SECRET_KEY_FILE+x}" = x ]; then
  provision_bucket_from_files
  exit 0
fi

: "${AELEV_INFRASTRUCTURE_MINIO_ENDPOINT:?set the MinIO endpoint}"
: "${AELEV_INFRASTRUCTURE_MINIO_PROVISION_BUCKET:?set the bucket to provision}"
: "${AELEV_INFRASTRUCTURE_MINIO_PROVISION_ACCESS_KEY:?set the provisioned access key}"
: "${AELEV_INFRASTRUCTURE_MINIO_PROVISION_SECRET_KEY:?set the provisioned secret key}"
: "${AELEV_INFRASTRUCTURE_MINIO_PROVISION_POLICY:?set the provisioned policy name}"
: "${MC_CONFIG_DIR:?set the temporary mc configuration directory}"

root_user_file=/run/secrets/minio-root-user
root_password_file=/run/secrets/minio-root-password
ca_file=/run/secrets/minio-ca.crt

for secret_file in "$root_user_file" "$root_password_file" "$ca_file"; do
  if [ ! -r "$secret_file" ] || [ ! -s "$secret_file" ]; then
    echo "required MinIO secret file is missing, empty, or unreadable: $secret_file" >&2
    exit 1
  fi
done

root_user=$(cat "$root_user_file")
root_password=$(cat "$root_password_file")
mkdir -p "$MC_CONFIG_DIR/certs/CAs"
cp "$ca_file" "$MC_CONFIG_DIR/certs/CAs/aelev-minio-ca.crt"

mc alias set infrastructure "$AELEV_INFRASTRUCTURE_MINIO_ENDPOINT" \
  "$root_user" \
  "$root_password" >/dev/null
unset root_user root_password
mc mb --ignore-existing "infrastructure/$AELEV_INFRASTRUCTURE_MINIO_PROVISION_BUCKET" >/dev/null
mc version enable "infrastructure/$AELEV_INFRASTRUCTURE_MINIO_PROVISION_BUCKET" >/dev/null
mc admin user add infrastructure \
  "$AELEV_INFRASTRUCTURE_MINIO_PROVISION_ACCESS_KEY" \
  "$AELEV_INFRASTRUCTURE_MINIO_PROVISION_SECRET_KEY" >/dev/null || true

cat > /tmp/policy.json <<EOF
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": ["s3:GetBucketLocation", "s3:ListBucket", "s3:ListBucketVersions"],
      "Resource": ["arn:aws:s3:::$AELEV_INFRASTRUCTURE_MINIO_PROVISION_BUCKET"]
    },
    {
      "Effect": "Allow",
      "Action": ["s3:GetObject", "s3:PutObject", "s3:DeleteObject", "s3:DeleteObjectVersion"],
      "Resource": ["arn:aws:s3:::$AELEV_INFRASTRUCTURE_MINIO_PROVISION_BUCKET/*"]
    }
  ]
}
EOF

mc admin policy create infrastructure \
  "$AELEV_INFRASTRUCTURE_MINIO_PROVISION_POLICY" /tmp/policy.json >/dev/null
mc admin policy attach infrastructure \
  "$AELEV_INFRASTRUCTURE_MINIO_PROVISION_POLICY" \
  --user "$AELEV_INFRASTRUCTURE_MINIO_PROVISION_ACCESS_KEY" >/dev/null
