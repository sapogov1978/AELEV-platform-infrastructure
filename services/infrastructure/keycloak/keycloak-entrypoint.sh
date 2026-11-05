#!/bin/sh
set -eu

read_secret() {
    secret_path="$1"
    secret_name="$2"
    if [ -z "$secret_path" ] || [ ! -f "$secret_path" ] || [ -L "$secret_path" ]; then
        echo "required secret file is unavailable: $secret_name" >&2
        exit 64
    fi
    secret_value=$(tr -d '\r\n' < "$secret_path")
    if [ -z "$secret_value" ]; then
        echo "required secret file is empty: $secret_name" >&2
        exit 64
    fi
    printf '%s' "$secret_value"
}

KC_BOOTSTRAP_ADMIN_PASSWORD=$(read_secret "${AELEV_KEYCLOAK_ADMIN_PASSWORD_FILE:-}" admin-password)
KC_DB_PASSWORD=$(read_secret "${AELEV_KEYCLOAK_DATABASE_PASSWORD_FILE:-}" database-password)
export KC_BOOTSTRAP_ADMIN_PASSWORD KC_DB_PASSWORD
unset AELEV_KEYCLOAK_ADMIN_PASSWORD_FILE AELEV_KEYCLOAK_DATABASE_PASSWORD_FILE

exec /opt/keycloak/bin/kc.sh "$@"
