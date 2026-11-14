#!/bin/sh
# Operator entry point. No final .env edits or runtime Docker socket.
set -eu
if [ "$#" -lt 4 ]; then
  echo 'Usage: install.sh server|agent|authorize|revoke|inspect|observation|observer|status-publisher|telemetry-sender|export-bootstrap PROJECT RUNTIME_IMAGE INSTALLER_IMAGE [options]' >&2
  exit 64
fi
spire_role=$1
spire_project=$2
spire_runtime_image=$3
spire_installer_image=$4
shift 4
if [ "${DRP_SPIRE_DOCKER_SOCKET_APPROVAL:-}" != 'operator-root-equivalent-v1' ]; then
  echo 'SPIRE_INSTALL_DOCKER_SOCKET_APPROVAL_REQUIRED' >&2
  exit 77
fi
case "$spire_role" in server|agent|authorize|revoke|inspect|observation|observer|status-publisher|telemetry-sender|export-bootstrap) ;; *) exit 64 ;; esac
case "$spire_project" in ''|*[!a-z0-9_-]*) exit 64 ;; esac
case $(uname -s) in Darwin) spire_host_os=Darwin ;; Linux) spire_host_os=Linux ;; *) echo 'USE_INSTALL_PS1' >&2; exit 78 ;; esac
spire_endpoint=${DOCKER_HOST:-$(docker context inspect --format '{{.Endpoints.docker.Host}}')}
case "$spire_endpoint" in unix://*) ;; *) echo 'SPIRE_INSTALL_LOCAL_DAEMON_REQUIRED' >&2; exit 78 ;; esac
if [ "$(docker info --format '{{.OSType}}')" != linux ]; then
  echo 'SPIRE_INSTALL_LINUX_ENGINE_REQUIRED' >&2
  exit 78
fi
spire_daemon=$(docker info --format '{{.ID}}')
for spire_image in "$spire_runtime_image" "$spire_installer_image"; do
  if ! docker image inspect "$spire_image" >/dev/null 2>&1; then docker pull "$spire_image"; fi
done
spire_state_volume="${spire_project}_installer"
if docker volume inspect "$spire_state_volume" >/dev/null 2>&1; then
  spire_volume_owner=$(docker volume inspect --format '{{index .Labels "io.aelev.owner"}}' "$spire_state_volume")
  spire_volume_installation=$(docker volume inspect --format '{{index .Labels "io.aelev.installation"}}' "$spire_state_volume")
  spire_volume_role=$(docker volume inspect --format '{{index .Labels "io.aelev.role"}}' "$spire_state_volume")
  if [ "$spire_volume_owner" != infrastructure-spire ] || [ "$spire_volume_installation" != "$spire_project" ] || [ "$spire_volume_role" != installer ]; then
    echo 'SPIRE_INSTALL_FOREIGN_VOLUME_REFUSED' >&2
    exit 78
  fi
else
  docker volume create --label io.aelev.owner=infrastructure-spire --label "io.aelev.installation=$spire_project" --label io.aelev.role=installer "$spire_state_volume" >/dev/null
fi
spire_empty_input=$(mktemp -d)
trap 'rmdir "$spire_empty_input"' EXIT
trap 'exit 130' INT
trap 'exit 143' HUP TERM
spire_input=${AELEV_INFRASTRUCTURE_SPIRE_INITIAL_INPUT:-$spire_empty_input}
spire_authority=${AELEV_INFRASTRUCTURE_SPIRE_AUTHORITY_SOURCE:-}
if [ -z "$spire_authority" ]; then
  spire_authority=$(docker run --rm --user 0:0 --network none --read-only --entrypoint python \
    --mount "type=volume,source=$spire_state_volume,target=/installation,readonly" \
    "$spire_runtime_image" -S -c 'import json; from pathlib import Path; p=Path("/installation/deployment.json"); print((json.loads(p.read_bytes()).get("authority_source") or "") if p.is_file() else "")')
fi
spire_authority=${spire_authority:-$spire_empty_input}
exec_status=0
docker run --rm --user 0:0 --read-only --network none --pids-limit 128 --memory 512m --cap-drop ALL --cap-add DAC_READ_SEARCH --security-opt no-new-privileges:true \
  --label io.aelev.owner=infrastructure-spire --label io.aelev.role=operator-installer --label "io.aelev.installation=$spire_project" \
  --tmpfs /tmp:size=16m,mode=1777 \
  --mount type=bind,source=/var/run/docker.sock,target=/var/run/docker.sock \
  --mount "type=volume,source=$spire_state_volume,target=/installation" \
  --mount "type=bind,source=$spire_input,target=/run/input,readonly" \
  --mount "type=bind,source=$spire_authority,target=/run/authority-source,readonly" \
  "$spire_installer_image" "$spire_role" --project "$spire_project" \
  --runtime-image "$spire_runtime_image" --daemon-id "$spire_daemon" --host-os "$spire_host_os" \
  --authority-source "$spire_authority" "$@" || exec_status=$?
exit "$exec_status"
