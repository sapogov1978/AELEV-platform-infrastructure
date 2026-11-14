#!/bin/sh
# Operator-only projection; never starts or recreates a provider role.
set -eu
umask 077
if [ "$#" -lt 2 ] || [ "$#" -gt 4 ]; then
  echo 'Usage: export.sh PROJECT EXPORT_OPERATOR_IMAGE [OUTPUT_ROOT] [COMPONENT]' >&2
  exit 64
fi
spire_export_project=$1
spire_export_image=$2
spire_export_component=${4-all}
case "$spire_export_component" in all|server|observer|status-publisher|telemetry-sender) ;; *) exit 64 ;; esac
if [ "${DRP_SPIRE_DOCKER_SOCKET_APPROVAL:-}" != 'operator-root-equivalent-v1' ]; then
  echo 'SPIRE_EXPORT_DOCKER_SOCKET_APPROVAL_REQUIRED' >&2
  exit 77
fi
case "$spire_export_project" in ''|*[!a-z0-9_-]*) exit 64 ;; esac
case $(uname -s) in Darwin|Linux) ;; *) echo 'USE_EXPORT_PS1' >&2; exit 78 ;; esac
if [ "$#" -ge 3 ]; then
  [ -n "$3" ] || exit 64
  spire_export_root=$3
else
  spire_export_root=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
fi
spire_export_endpoint=${DOCKER_HOST:-$(docker context inspect --format '{{.Endpoints.docker.Host}}')}
case "$spire_export_endpoint" in unix://*) ;; *) echo 'SPIRE_EXPORT_LOCAL_DAEMON_REQUIRED' >&2; exit 78 ;; esac
if [ "$(docker info --format '{{.OSType}}')" != linux ]; then exit 78; fi
spire_export_daemon=$(docker info --format '{{.ID}}')
spire_export_volume="${spire_export_project}_installer"
if [ "$(docker volume inspect --format '{{index .Labels "io.aelev.owner"}}' "$spire_export_volume")" != infrastructure-spire ] ||
   [ "$(docker volume inspect --format '{{index .Labels "io.aelev.installation"}}' "$spire_export_volume")" != "$spire_export_project" ] ||
   [ "$(docker volume inspect --format '{{index .Labels "io.aelev.role"}}' "$spire_export_volume")" != installer ]; then
  echo 'SPIRE_EXPORT_FOREIGN_STATE_REFUSED' >&2
  exit 78
fi
mkdir -p -- "$spire_export_root"
spire_export_root=$(CDPATH= cd -- "$spire_export_root" && pwd)
# A manually maintained .env may contain other settings: never replace it.
if [ -e "$spire_export_root/.env" ]; then
  if [ -L "$spire_export_root/.env" ] || [ "$(head -n 1 "$spire_export_root/.env")" != '# AELEV SPIRE installed composition v1' ] ||
     [ "$(wc -l < "$spire_export_root/.env" | tr -d ' ')" != 2 ] ||
     ! tail -n 1 "$spire_export_root/.env" | grep -Eq '^AELEV_INFRASTRUCTURE_SPIRE_INSTALLED_COMPOSE=\./runtime/installed-composition/[a-f0-9]{64}/compose\.json$'; then
    echo 'SPIRE_EXPORT_EXISTING_ENV_PRESERVED' >&2
    exit 78
  fi
fi
mkdir -p "$spire_export_root/runtime/installed-composition"
spire_export_before=$(mktemp "$spire_export_root/runtime/installed-composition/env-before.XXXXXX")
spire_export_next=$(mktemp "$spire_export_root/runtime/installed-composition/env-next.XXXXXX")
spire_export_helper="${spire_export_project}-composition-$(basename -- "$spire_export_next")"
trap 'rm -f "$spire_export_before" "$spire_export_next"' EXIT
if [ -e "$spire_export_root/.env" ]; then cp "$spire_export_root/.env" "$spire_export_before"; fi
set --
if [ "$spire_export_component" != all ]; then set -- --component "$spire_export_component"; fi
docker run --rm --name "$spire_export_helper" --user 0:0 --read-only --network none --pids-limit 128 --cap-drop ALL --security-opt no-new-privileges:true \
  --label io.aelev.owner=infrastructure-spire --label io.aelev.role=operator-composition-export \
  --label "io.aelev.installation=$spire_export_project" --memory 256m --tmpfs /tmp:mode=1777,size=16m \
  --mount type=bind,source=/var/run/docker.sock,target=/var/run/docker.sock \
  --mount "type=volume,source=$spire_export_volume,target=/installation,readonly,volume-nocopy" \
  --mount "type=bind,source=$spire_export_root/runtime/installed-composition,target=/output" \
  --entrypoint python "$spire_export_image" -m aelev_spire.composition \
  --project "$spire_export_project" --daemon-id "$spire_export_daemon" --output /output "$@"
cp "$spire_export_root/runtime/installed-composition/binding.env" "$spire_export_next"
if [ -e "$spire_export_root/.env" ]; then
  cmp -s "$spire_export_root/.env" "$spire_export_before" || { echo 'SPIRE_EXPORT_ENV_CHANGED' >&2; exit 78; }
else
  [ ! -s "$spire_export_before" ] || exit 78
fi
mv "$spire_export_next" "$spire_export_root/.env"
