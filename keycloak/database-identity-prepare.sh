#!/bin/sh
# Run only on the SQL role's own node, using its pinned PostgreSQL image.
set -eu
umask 077

source_root=/run/secrets/keycloak-database-server
target_root=/var/lib/keycloak-database-tls
fail() { echo "Keycloak database identity preparation refused: $1" >&2; exit 64; }

[ "$(id -u)" = 0 ] || fail root-required
[ "$(id -u postgres):$(id -g postgres)" = "70:70" ] || fail pinned-postgres-uid
[ -d "$source_root" ] && [ ! -L "$source_root" ] || fail source-directory
[ -d "$target_root" ] && [ ! -L "$target_root" ] || fail target-volume
stage="$target_root/.prepare-$$"
mkdir "$target_root/.prepare-lock" 2>/dev/null || fail concurrent-or-interrupted-prepare
trap 'rm -rf "$stage"; rmdir "$target_root/.prepare-lock"' EXIT HUP INT TERM

for name in server.crt server.key client-ca.crt; do
    source_file="$source_root/$name"
    [ -f "$source_file" ] && [ ! -L "$source_file" ] && [ -s "$source_file" ] || fail source-file
    [ "$(wc -c < "$source_file")" -le 1048576 ] || fail source-size
done

# Refuse drift or unexpected state before changing any existing directory/file.
entries=$(find "$target_root" -mindepth 1 -maxdepth 1 ! -name .prepare-lock | wc -l)
if [ "$entries" -ne 0 ]; then
    [ "$(stat -c '%u:%g:%a' "$target_root")" = "70:70:700" ] || fail target-directory-mode
    for name in server.crt server.key client-ca.crt; do
        target_file="$target_root/$name"
        [ -f "$target_file" ] && [ ! -L "$target_file" ] || fail incomplete-target
        [ "$(stat -c '%u:%g:%a' "$target_file")" = "70:70:600" ] || fail target-file-mode
        cmp -s "$source_root/$name" "$target_file" || fail identity-drift
    done
    [ "$entries" -eq 3 ] || fail unexpected-target-file
    echo KEYCLOAK_DATABASE_IDENTITY_UNCHANGED
    exit 0
fi

# Publish the complete set only after all source and empty-target checks.
mkdir "$stage"
for name in server.crt server.key client-ca.crt; do
    cp "$source_root/$name" "$stage/$name"
    chown 70:70 "$stage/$name"
    chmod 0600 "$stage/$name"
done
for name in server.crt server.key client-ca.crt; do
    mv "$stage/$name" "$target_root/$name"
done
rmdir "$stage"
chown 70:70 "$target_root"
chmod 0700 "$target_root"
echo KEYCLOAK_DATABASE_IDENTITY_PREPARED
