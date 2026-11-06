#!/bin/sh
set -eu
umask 077
mkdir -p /run/aelev/secrets
cp /run/secrets/postgresql/* /run/aelev/secrets/
chown -R postgres:postgres /run/aelev
chmod 600 /run/aelev/secrets/*
if [ "$1" = start ]; then
  mkdir -p /run/aelev /var/lib/postgresql/data
  chown postgres:postgres /run/aelev /var/lib/postgresql/data
  exec su-exec postgres python -m aelev_postgresql "$@"
fi
exec python -m aelev_postgresql "$@"
