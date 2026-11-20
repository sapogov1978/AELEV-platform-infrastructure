#!/bin/sh
# Initial server identity handoff on this SQL role's assigned node only.
set -eu
umask 077
source_root=/run/secrets/translator-database-server
target_root=/var/lib/translator-database-tls
fail() { echo "Translator database identity preparation refused: $1" >&2; exit 64; }
[ "$(id -u)" = 0 ] || fail root-required
postgres_uid=$(id -u postgres)
postgres_gid=$(id -g postgres)
case "$postgres_uid:$postgres_gid" in *[!0-9:]* | 0:* | *:0) fail postgres-identity ;; esac
[ -d "$source_root" ] && [ ! -L "$source_root" ] || fail source-directory
[ -d "$target_root" ] && [ ! -L "$target_root" ] || fail target-volume
source_mode=$(stat -c '%a' "$source_root")
[ "$((0$source_mode & 0022))" -eq 0 ] || fail source-directory-mode

for name in server.crt server.key client-ca.crt; do
    source_file="$source_root/$name"
    [ -f "$source_file" ] && [ ! -L "$source_file" ] && [ -s "$source_file" ] || fail source-file
    [ "$(stat -c '%h' "$source_file")" = 1 ] || fail source-link
    [ "$(wc -c < "$source_file")" -le 1048576 ] || fail source-size
    case "$(stat -c '%a' "$source_file")" in 600 | 640) ;; *) fail source-file-mode ;; esac
done

stage="$target_root/.prepare-$$"
lock="$target_root/.prepare-lock"
stage_owned=false
mkdir "$lock" 2>/dev/null || fail concurrent-or-interrupted-prepare
# A refusal must never remove a stage left by another or interrupted invocation.
cleanup() {
    if [ "$stage_owned" = true ]; then
        rm -rf "$stage"
    fi
    rmdir "$lock"
}
trap cleanup EXIT
trap 'exit 130' HUP INT TERM
entries=$(find "$target_root" -mindepth 1 -maxdepth 1 ! -name .prepare-lock | wc -l)
if [ "$entries" -ne 0 ]; then
    [ "$(stat -c '%u:%g:%a' "$target_root")" = "$postgres_uid:$postgres_gid:700" ] || fail target-directory-mode
    [ "$entries" -eq 3 ] || fail incomplete-or-unexpected-target
    for name in server.crt server.key client-ca.crt; do
        target_file="$target_root/$name"
        [ -f "$target_file" ] && [ ! -L "$target_file" ] || fail target-file
        [ "$(stat -c '%h' "$target_file")" = 1 ] || fail target-link
        [ "$(stat -c '%u:%g:%a' "$target_file")" = "$postgres_uid:$postgres_gid:600" ] || fail target-file-mode
        cmp -s "$source_root/$name" "$target_file" || fail identity-drift
    done
    echo TRANSLATOR_DATABASE_IDENTITY_UNCHANGED
    exit 0
fi
[ "$(stat -c '%u' "$target_root")" = 0 ] || fail fresh-target-owner
target_mode=$(stat -c '%a' "$target_root")
[ "$((0$target_mode & 0022))" -eq 0 ] || fail fresh-target-mode
mkdir "$stage"
stage_owned=true
for name in server.crt server.key client-ca.crt; do
    cp "$source_root/$name" "$stage/$name"
    chown "$postgres_uid:$postgres_gid" "$stage/$name"
    chmod 0600 "$stage/$name"
done
for name in server.crt server.key client-ca.crt; do
    mv "$stage/$name" "$target_root/$name"
done
rmdir "$stage"
stage_owned=false
chown "$postgres_uid:$postgres_gid" "$target_root"
chmod 0700 "$target_root"
sync -f "$target_root"
echo TRANSLATOR_DATABASE_IDENTITY_PREPARED
