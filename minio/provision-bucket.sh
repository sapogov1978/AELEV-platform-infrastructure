#!/bin/sh
set -eu

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
