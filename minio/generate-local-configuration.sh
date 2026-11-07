#!/bin/sh
set -eu

umask 077
component_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
env_file=$component_dir/.env
secrets_dir=$component_dir/secrets

if ! openssl version >/dev/null 2>&1; then
  echo "OpenSSL is not installed or cannot start" >&2
  exit 1
fi
if ! openssl req -help 2>&1 | grep -q -- "-addext"; then
  echo "OpenSSL does not support req -addext; install OpenSSL 1.1.1 or newer" >&2
  exit 1
fi

for target in "$env_file" "$secrets_dir/root-user" "$secrets_dir/root-password" "$secrets_dir/site-a/private.key" "$secrets_dir/site-a/public.crt" "$secrets_dir/site-a/CAs/aelev-minio-sites.crt" "$secrets_dir/site-b/private.key" "$secrets_dir/site-b/public.crt" "$secrets_dir/site-b/CAs/aelev-minio-sites.crt" "$secrets_dir/sites-ca.crt"; do
  if [ -e "$target" ]; then
    echo "refusing to overwrite existing local configuration: $target" >&2
    exit 1
  fi
done

mkdir -p "$secrets_dir/site-a/CAs" "$secrets_dir/site-b/CAs"
cp "$component_dir/.env.example" "$env_file"
openssl rand -hex 16 > "$secrets_dir/root-user"
openssl rand -hex 32 > "$secrets_dir/root-password"
openssl req \
  -x509 \
  -newkey rsa:3072 \
  -sha256 \
  -days 365 \
  -nodes \
  -keyout "$secrets_dir/site-a/private.key" \
  -out "$secrets_dir/site-a/public.crt" \
  -subj "/CN=minio-site-a" \
  -addext "subjectAltName=DNS:minio,DNS:minio-site-a,DNS:minio-site-a-1,DNS:minio-site-a-2,DNS:minio-site-a-3,DNS:minio-site-a-4,DNS:localhost,IP:127.0.0.1"
openssl req \
  -x509 \
  -newkey rsa:3072 \
  -sha256 \
  -days 365 \
  -nodes \
  -keyout "$secrets_dir/site-b/private.key" \
  -out "$secrets_dir/site-b/public.crt" \
  -subj "/CN=minio-site-b" \
  -addext "subjectAltName=DNS:minio,DNS:minio-site-b,DNS:minio-site-b-1,DNS:minio-site-b-2,DNS:minio-site-b-3,DNS:minio-site-b-4,DNS:localhost,IP:127.0.0.1"
cp "$secrets_dir/site-a/public.crt" "$secrets_dir/sites-ca.crt"
cat "$secrets_dir/site-b/public.crt" >> "$secrets_dir/sites-ca.crt"
cp "$secrets_dir/sites-ca.crt" "$secrets_dir/site-a/CAs/aelev-minio-sites.crt"
cp "$secrets_dir/sites-ca.crt" "$secrets_dir/site-b/CAs/aelev-minio-sites.crt"
chmod 600 "$env_file" "$secrets_dir/root-user" "$secrets_dir/root-password" "$secrets_dir/site-a/private.key" "$secrets_dir/site-b/private.key"
chmod 644 "$secrets_dir/site-a/public.crt" "$secrets_dir/site-b/public.crt" "$secrets_dir/site-a/CAs/aelev-minio-sites.crt" "$secrets_dir/site-b/CAs/aelev-minio-sites.crt" "$secrets_dir/sites-ca.crt"

echo "Local MinIO configuration was created without printing credentials."
