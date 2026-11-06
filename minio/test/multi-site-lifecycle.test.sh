#!/usr/bin/env bash
set -Eeuo pipefail

script_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
minio_dir=$(cd "$script_dir/.." && pwd)
server_image=aelev/minio-server:RELEASE.2025-04-22T22-12-26Z
load_balancer_image=nginx:1.29.1-alpine
file_mib=${AELEV_MINIO_TEST_FILE_MIB:-2048}
upload_limit=${AELEV_MINIO_TEST_UPLOAD_LIMIT:-8Mi}
health_attempts=${AELEV_MINIO_TEST_HEALTH_ATTEMPTS:-120}
replication_attempts=${AELEV_MINIO_TEST_REPLICATION_ATTEMPTS:-180}

nodes=(
  minio-site-a-1 minio-site-a-2 minio-site-a-3 minio-site-a-4
  minio-site-b-1 minio-site-b-2 minio-site-b-3 minio-site-b-4
)
load_balancers=(minio-site-a-lb minio-site-b-lb)

fail() {
  echo "FAIL: $*" >&2
  exit 1
}

for command_name in docker openssl dd shasum awk mktemp; do
  command -v "$command_name" >/dev/null 2>&1 || fail "required command is unavailable: $command_name"
done
docker info >/dev/null 2>&1 || fail "Docker daemon is unavailable"
case "$file_mib" in
  ''|*[!0-9]*) fail "AELEV_MINIO_TEST_FILE_MIB must be a positive integer" ;;
esac
[ "$file_mib" -gt 0 ] || fail "AELEV_MINIO_TEST_FILE_MIB must be greater than zero"
case "$health_attempts" in
  ''|*[!0-9]*) fail "AELEV_MINIO_TEST_HEALTH_ATTEMPTS must be a positive integer" ;;
esac
case "$replication_attempts" in
  ''|*[!0-9]*) fail "AELEV_MINIO_TEST_REPLICATION_ATTEMPTS must be a positive integer" ;;
esac

run_id=$(date -u +%Y%m%d%H%M%S)-$$
test_parent=${TMPDIR:-/tmp}
test_parent=${test_parent%/}
test_work_dir=$(mktemp -d "$test_parent/aelev-minio-test.XXXXXX")
case "$test_work_dir" in
  "$test_parent"/aelev-minio-test.*) ;;
  *) fail "mktemp returned an unexpected directory: $test_work_dir" ;;
esac

export AELEV_INFRASTRUCTURE_MINIO_COMPOSE_PROJECT="aelev-minio-test-$run_id"
export AELEV_INFRASTRUCTURE_MINIO_UI_STATE_VOLUME="$AELEV_INFRASTRUCTURE_MINIO_COMPOSE_PROJECT-status"
export AELEV_INFRASTRUCTURE_MINIO_SITE_A_ENDPOINT=https://minio-site-a-lb:9000
export AELEV_INFRASTRUCTURE_MINIO_SITE_B_ENDPOINT=https://minio-site-b-lb:9000
export AELEV_INFRASTRUCTURE_MINIO_BIND=127.0.0.1
export AELEV_INFRASTRUCTURE_MINIO_SITE_A_NODE_1_PORT=19001
export AELEV_INFRASTRUCTURE_MINIO_SITE_A_NODE_2_PORT=19002
export AELEV_INFRASTRUCTURE_MINIO_SITE_A_NODE_3_PORT=19003
export AELEV_INFRASTRUCTURE_MINIO_SITE_A_NODE_4_PORT=19004
export AELEV_INFRASTRUCTURE_MINIO_SITE_B_NODE_1_PORT=19005
export AELEV_INFRASTRUCTURE_MINIO_SITE_B_NODE_2_PORT=19006
export AELEV_INFRASTRUCTURE_MINIO_SITE_B_NODE_3_PORT=19007
export AELEV_INFRASTRUCTURE_MINIO_SITE_B_NODE_4_PORT=19008
export AELEV_INFRASTRUCTURE_MINIO_SITE_A_NODE_1_ENDPOINT=https://minio-site-a-1:9000/data
export AELEV_INFRASTRUCTURE_MINIO_SITE_A_NODE_2_ENDPOINT=https://minio-site-a-2:9000/data
export AELEV_INFRASTRUCTURE_MINIO_SITE_A_NODE_3_ENDPOINT=https://minio-site-a-3:9000/data
export AELEV_INFRASTRUCTURE_MINIO_SITE_A_NODE_4_ENDPOINT=https://minio-site-a-4:9000/data
export AELEV_INFRASTRUCTURE_MINIO_SITE_B_NODE_1_ENDPOINT=https://minio-site-b-1:9000/data
export AELEV_INFRASTRUCTURE_MINIO_SITE_B_NODE_2_ENDPOINT=https://minio-site-b-2:9000/data
export AELEV_INFRASTRUCTURE_MINIO_SITE_B_NODE_3_ENDPOINT=https://minio-site-b-3:9000/data
export AELEV_INFRASTRUCTURE_MINIO_SITE_B_NODE_4_ENDPOINT=https://minio-site-b-4:9000/data
export AELEV_INFRASTRUCTURE_MINIO_SITE_A_NODE_1_VOLUME="$AELEV_INFRASTRUCTURE_MINIO_COMPOSE_PROJECT-site-a-1-data"
export AELEV_INFRASTRUCTURE_MINIO_SITE_A_NODE_2_VOLUME="$AELEV_INFRASTRUCTURE_MINIO_COMPOSE_PROJECT-site-a-2-data"
export AELEV_INFRASTRUCTURE_MINIO_SITE_A_NODE_3_VOLUME="$AELEV_INFRASTRUCTURE_MINIO_COMPOSE_PROJECT-site-a-3-data"
export AELEV_INFRASTRUCTURE_MINIO_SITE_A_NODE_4_VOLUME="$AELEV_INFRASTRUCTURE_MINIO_COMPOSE_PROJECT-site-a-4-data"
export AELEV_INFRASTRUCTURE_MINIO_SITE_B_NODE_1_VOLUME="$AELEV_INFRASTRUCTURE_MINIO_COMPOSE_PROJECT-site-b-1-data"
export AELEV_INFRASTRUCTURE_MINIO_SITE_B_NODE_2_VOLUME="$AELEV_INFRASTRUCTURE_MINIO_COMPOSE_PROJECT-site-b-2-data"
export AELEV_INFRASTRUCTURE_MINIO_SITE_B_NODE_3_VOLUME="$AELEV_INFRASTRUCTURE_MINIO_COMPOSE_PROJECT-site-b-3-data"
export AELEV_INFRASTRUCTURE_MINIO_SITE_B_NODE_4_VOLUME="$AELEV_INFRASTRUCTURE_MINIO_COMPOSE_PROJECT-site-b-4-data"
export AELEV_INFRASTRUCTURE_MINIO_PARITY=EC:2
export AELEV_INFRASTRUCTURE_MINIO_NETWORK="$AELEV_INFRASTRUCTURE_MINIO_COMPOSE_PROJECT-network"
export AELEV_INFRASTRUCTURE_MINIO_INGRESS_NETWORK="$AELEV_INFRASTRUCTURE_MINIO_COMPOSE_PROJECT-ingress"
export AELEV_INFRASTRUCTURE_MINIO_PROVISION_BUCKET="aelev-minio-test-$run_id"
export AELEV_INFRASTRUCTURE_MINIO_PROVISION_ACCESS_KEY="test$(openssl rand -hex 8)"
export AELEV_INFRASTRUCTURE_MINIO_PROVISION_SECRET_KEY="$(openssl rand -hex 24)"
export AELEV_INFRASTRUCTURE_MINIO_PROVISION_POLICY="aelev-minio-test-$run_id"
export AELEV_MINIO_TEST_ROOT_USER_FILE="$test_work_dir/root-user"
export AELEV_MINIO_TEST_ROOT_PASSWORD_FILE="$test_work_dir/root-password"
export AELEV_MINIO_TEST_SITE_A_CERTIFICATE="$test_work_dir/site-a/public.crt"
export AELEV_MINIO_TEST_SITE_A_PRIVATE_KEY="$test_work_dir/site-a/private.key"
export AELEV_MINIO_TEST_SITE_B_CERTIFICATE="$test_work_dir/site-b/public.crt"
export AELEV_MINIO_TEST_SITE_B_PRIVATE_KEY="$test_work_dir/site-b/private.key"
export AELEV_MINIO_TEST_SITES_CA_FILE="$test_work_dir/sites-ca.crt"
export AELEV_MINIO_TEST_WORK_DIR="$test_work_dir"
export AELEV_MINIO_TEST_ACCESS_KEY="$AELEV_INFRASTRUCTURE_MINIO_PROVISION_ACCESS_KEY"
export AELEV_MINIO_TEST_SECRET_KEY="$AELEV_INFRASTRUCTURE_MINIO_PROVISION_SECRET_KEY"
export AELEV_MINIO_TEST_OBJECT="large-$run_id.bin"
export AELEV_MINIO_TEST_UPLOAD_LIMIT="$upload_limit"

compose=(
  docker compose
  --env-file "$minio_dir/.env.example"
  -f "$minio_dir/compose.yml"
  -f "$script_dir/compose.test.yml"
)

server_image_preexisting=0
docker image inspect "$server_image" >/dev/null 2>&1 && server_image_preexisting=1
load_balancer_image_preexisting=0
docker image inspect "$load_balancer_image" >/dev/null 2>&1 && load_balancer_image_preexisting=1
cleanup_done=0

cleanup_resources() {
  local cleanup_status=0
  set +e
  "${compose[@]}" --profile provision --profile test down --volumes --remove-orphans --timeout 10 >/dev/null 2>&1 || cleanup_status=1
  if [ "$server_image_preexisting" -eq 0 ] && docker image inspect "$server_image" >/dev/null 2>&1; then
    docker image rm "$server_image" >/dev/null 2>&1 || cleanup_status=1
  fi
  if [ "$load_balancer_image_preexisting" -eq 0 ] && docker image inspect "$load_balancer_image" >/dev/null 2>&1; then
    docker image rm "$load_balancer_image" >/dev/null 2>&1 || cleanup_status=1
  fi
  case "$test_work_dir" in
    "$test_parent"/aelev-minio-test.*) rm -rf -- "$test_work_dir" || cleanup_status=1 ;;
    *) cleanup_status=1 ;;
  esac
  set -e
  return "$cleanup_status"
}

on_exit() {
  local status=$?
  trap - EXIT INT TERM
  if [ "$cleanup_done" -eq 0 ]; then
    if [ "$status" -ne 0 ]; then
      echo "Diagnostic tail from MinIO containers:" >&2
      "${compose[@]}" logs --no-color --tail 60 "${nodes[@]}" >&2 || true
    fi
    cleanup_resources || {
      echo "FAIL: cleanup did not complete" >&2
      [ "$status" -ne 0 ] || status=1
    }
  fi
  exit "$status"
}
trap on_exit EXIT INT TERM

wait_for_health() {
  local service=$1
  local attempt container_id health
  attempt=1
  while [ "$attempt" -le "$health_attempts" ]; do
    container_id=$("${compose[@]}" ps -q "$service" 2>/dev/null || true)
    if [ -n "$container_id" ]; then
      health=$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$container_id" 2>/dev/null || true)
      [ "$health" = healthy ] && return 0
    fi
    sleep 2
    attempt=$((attempt + 1))
  done
  if [ -n "${container_id:-}" ]; then
    docker inspect --format '{{range .State.Health.Log}}{{.End}} exit={{.ExitCode}} {{printf "%s" .Output}}{{println}}{{end}}' "$container_id" >&2 || true
  fi
  fail "$service did not become healthy after $health_attempts attempts"
}

wait_for_all_health() {
  local node
  for node in "${nodes[@]}"; do
    wait_for_health "$node"
  done
}

assert_stopped() {
  local service=$1
  local container_id running
  container_id=$("${compose[@]}" ps -q --all "$service")
  [ -n "$container_id" ] || fail "$service container does not exist"
  running=$(docker inspect --format '{{.State.Running}}' "$container_id")
  [ "$running" = false ] || fail "$service remained running after stop"
}

run_client() {
  "${compose[@]}" --profile test run --rm --no-deps -T test-client "$@"
}

wait_for_write_quorum_loss() {
  local service=$1
  local attempt=1 container_id health
  container_id=$("${compose[@]}" ps -q "$service")
  [ -n "$container_id" ] || fail "$service container does not exist"
  while [ "$attempt" -le "$health_attempts" ]; do
    health=$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$container_id" 2>/dev/null || true)
    [ "$health" = unhealthy ] && return 0
    sleep 2
    attempt=$((attempt + 1))
  done
  fail "$service continued reporting write quorum after $health_attempts attempts"
}

fail_ended_upload() {
  local stage=$1 upload_status
  set +e
  wait "$upload_pid"
  upload_status=$?
  set -e
  sed -n '1,120p' "$test_work_dir/upload.log" >&2
  if [ "$upload_status" -eq 0 ]; then
    fail "large-object upload completed before $stage"
  fi
  fail "large-object upload failed before $stage with exit status $upload_status"
}

wait_for_bucket_access() {
  local site=$1
  local attempt=1
  while [ "$attempt" -le "$replication_attempts" ]; do
    if run_client mc stat "$site/$AELEV_INFRASTRUCTURE_MINIO_PROVISION_BUCKET" >/dev/null 2>&1; then
      return 0
    fi
    sleep 2
    attempt=$((attempt + 1))
  done
  fail "provisioned bucket did not become accessible from $site"
}

mkdir -p "$test_work_dir/site-a" "$test_work_dir/site-b"
umask 077
openssl rand -hex 16 > "$AELEV_MINIO_TEST_ROOT_USER_FILE"
openssl rand -hex 32 > "$AELEV_MINIO_TEST_ROOT_PASSWORD_FILE"
openssl req -x509 -nodes -newkey rsa:2048 -days 1 -sha256 \
  -keyout "$AELEV_MINIO_TEST_SITE_A_PRIVATE_KEY" \
  -out "$AELEV_MINIO_TEST_SITE_A_CERTIFICATE" \
  -subj "/CN=minio-site-a" \
  -addext "subjectAltName=DNS:minio-site-a,DNS:minio-site-a-lb,DNS:minio-site-a-1,DNS:minio-site-a-2,DNS:minio-site-a-3,DNS:minio-site-a-4,DNS:localhost,IP:127.0.0.1" \
  >/dev/null 2>&1
openssl req -x509 -nodes -newkey rsa:2048 -days 1 -sha256 \
  -keyout "$AELEV_MINIO_TEST_SITE_B_PRIVATE_KEY" \
  -out "$AELEV_MINIO_TEST_SITE_B_CERTIFICATE" \
  -subj "/CN=minio-site-b" \
  -addext "subjectAltName=DNS:minio-site-b,DNS:minio-site-b-lb,DNS:minio-site-b-1,DNS:minio-site-b-2,DNS:minio-site-b-3,DNS:minio-site-b-4,DNS:localhost,IP:127.0.0.1" \
  >/dev/null 2>&1
{
  sed -n '/-----BEGIN CERTIFICATE-----/,/-----END CERTIFICATE-----/p' "$AELEV_MINIO_TEST_SITE_A_CERTIFICATE"
  sed -n '/-----BEGIN CERTIFICATE-----/,/-----END CERTIFICATE-----/p' "$AELEV_MINIO_TEST_SITE_B_CERTIFICATE"
} > "$AELEV_MINIO_TEST_SITES_CA_FILE"
chmod 600 "$AELEV_MINIO_TEST_ROOT_USER_FILE" "$AELEV_MINIO_TEST_ROOT_PASSWORD_FILE" \
  "$AELEV_MINIO_TEST_SITE_A_PRIVATE_KEY" "$AELEV_MINIO_TEST_SITE_B_PRIVATE_KEY"
chmod 644 "$AELEV_MINIO_TEST_SITE_A_CERTIFICATE" "$AELEV_MINIO_TEST_SITE_B_CERTIFICATE" "$AELEV_MINIO_TEST_SITES_CA_FILE"

echo "Creating ${file_mib} MiB test object..."
dd if=/dev/zero of="$test_work_dir/large.bin" bs=1048576 count="$file_mib" status=none
source_sha256=$(shasum -a 256 "$test_work_dir/large.bin" | awk '{print $1}')

"${compose[@]}" config --quiet
echo "Starting eight MinIO nodes..."
"${compose[@]}" build minio-site-a-1
minio_version=$(docker run --rm "$server_image" --version)
case "$minio_version" in
  *"RELEASE.2025-04-22T22-12-26Z"*"commit-id=0d7408fc9969caf07de6a8c3a84f9fbb10a6739e"*) ;;
  *) fail "built MinIO server has an unexpected version: $minio_version" ;;
esac
mc_version=$(docker run --rm --entrypoint mc "$server_image" --version)
case "$mc_version" in
  *"RELEASE.2025-04-16T18-13-26Z"*"commit-id=b00526b153a31b36767991a4f5ce2cced435ee8e"*) ;;
  *) fail "built MinIO client has an unexpected version: $mc_version" ;;
esac
echo "PASS: MinIO server and client versions match their pinned releases and commits"
"${compose[@]}" --profile test up -d --pull missing "${nodes[@]}" "${load_balancers[@]}"
wait_for_all_health
echo "PASS: all 8 nodes are healthy"

echo "Configuring site replication and the test-owned bucket..."
"${compose[@]}" --profile provision run --rm --no-deps -T site-replication-applier >/dev/null
"${compose[@]}" --profile provision run --rm --no-deps -T bucket-provisioner >/dev/null
wait_for_bucket_access site-a
wait_for_bucket_access site-b
echo "PASS: site replication and replicated access are available"

echo "Starting throttled large-object upload..."
run_client upload 30 >"$test_work_dir/upload.log" 2>&1 &
upload_pid=$!
sleep 5
kill -0 "$upload_pid" 2>/dev/null || {
  fail_ended_upload "fault injection began"
}

cycled_nodes=0
for node in "${nodes[@]}"; do
  kill -0 "$upload_pid" 2>/dev/null || {
    fail_ended_upload "all nodes were cycled"
  }
  echo "Cycling $node while upload is active..."
  "${compose[@]}" stop --timeout 8 "$node" >/dev/null
  assert_stopped "$node"
  kill -0 "$upload_pid" 2>/dev/null || {
    fail_ended_upload "$node was restored"
  }
  sleep 3
  "${compose[@]}" up -d --no-deps --no-recreate "$node" >/dev/null
  wait_for_health "$node"
  cycled_nodes=$((cycled_nodes + 1))
done
wait "$upload_pid" || {
  sed -n '1,120p' "$test_work_dir/upload.log" >&2
  fail "large-object upload failed during sequential node outages"
}
[ "$cycled_nodes" -eq 8 ] || fail "not every node was cycled during upload"
echo "PASS: upload completed while all 8 nodes were stopped and restored one at a time"

run_client wait-object site-a "$replication_attempts"
run_client wait-object site-b "$replication_attempts"
site_a_sha256=$(run_client hash site-a | tail -n 1)
site_b_sha256=$(run_client hash site-b | tail -n 1)
[ "$site_a_sha256" = "$source_sha256" ] || fail "site A object SHA-256 differs from the source"
[ "$site_b_sha256" = "$source_sha256" ] || fail "site B object SHA-256 differs from the source"
echo "PASS: source and both sites have SHA-256 $source_sha256"

echo "Checking fail-closed behavior after loss of write quorum at site A..."
"${compose[@]}" stop --timeout 8 minio-site-a-1 minio-site-a-2 >/dev/null
assert_stopped minio-site-a-1
assert_stopped minio-site-a-2
wait_for_write_quorum_loss minio-site-a-3
echo "PASS: site A fails its cluster health check after losing write quorum"
"${compose[@]}" up -d --no-deps --no-recreate minio-site-a-1 minio-site-a-2 >/dev/null
wait_for_all_health

echo "Stopping and restarting the complete topology..."
"${compose[@]}" stop --timeout 10 "${nodes[@]}" "${load_balancers[@]}" >/dev/null
for service in "${nodes[@]}" "${load_balancers[@]}"; do
  assert_stopped "$service"
done
"${compose[@]}" --profile test up -d --no-recreate "${nodes[@]}" "${load_balancers[@]}" >/dev/null
wait_for_all_health
site_a_sha256_after_restart=$(run_client hash site-a | tail -n 1)
site_b_sha256_after_restart=$(run_client hash site-b | tail -n 1)
[ "$site_a_sha256_after_restart" = "$source_sha256" ] || fail "site A lost or changed the object after restart"
[ "$site_b_sha256_after_restart" = "$source_sha256" ] || fail "site B lost or changed the object after restart"
echo "PASS: both sites retained the object across a complete stop and restart"

"${compose[@]}" --profile provision run --rm --no-deps -T site-replication-applier >/dev/null
echo "PASS: site-replication configuration is idempotent after restart"

echo "Stopping the topology before cleanup..."
"${compose[@]}" stop --timeout 10 "${nodes[@]}" "${load_balancers[@]}" >/dev/null
for service in "${nodes[@]}" "${load_balancers[@]}"; do
  assert_stopped "$service"
done
echo "PASS: all 8 nodes and both test load balancers stopped cleanly"

cleanup_resources || fail "cleanup did not complete"
cleanup_done=1
[ -z "$(docker ps -a --filter "label=com.docker.compose.project=$AELEV_INFRASTRUCTURE_MINIO_COMPOSE_PROJECT" --format '{{.ID}}')" ] || fail "test containers remain after cleanup"
for volume_name in \
  "$AELEV_INFRASTRUCTURE_MINIO_SITE_A_NODE_1_VOLUME" \
  "$AELEV_INFRASTRUCTURE_MINIO_SITE_A_NODE_2_VOLUME" \
  "$AELEV_INFRASTRUCTURE_MINIO_SITE_A_NODE_3_VOLUME" \
  "$AELEV_INFRASTRUCTURE_MINIO_SITE_A_NODE_4_VOLUME" \
  "$AELEV_INFRASTRUCTURE_MINIO_SITE_B_NODE_1_VOLUME" \
  "$AELEV_INFRASTRUCTURE_MINIO_SITE_B_NODE_2_VOLUME" \
  "$AELEV_INFRASTRUCTURE_MINIO_SITE_B_NODE_3_VOLUME" \
  "$AELEV_INFRASTRUCTURE_MINIO_SITE_B_NODE_4_VOLUME"; do
  docker volume inspect "$volume_name" >/dev/null 2>&1 && fail "test volume remains after cleanup: $volume_name"
done
docker network inspect "$AELEV_INFRASTRUCTURE_MINIO_NETWORK" >/dev/null 2>&1 && fail "test network remains after cleanup: $AELEV_INFRASTRUCTURE_MINIO_NETWORK"
docker network inspect "$AELEV_INFRASTRUCTURE_MINIO_INGRESS_NETWORK" >/dev/null 2>&1 && fail "test network remains after cleanup: $AELEV_INFRASTRUCTURE_MINIO_INGRESS_NETWORK"
[ ! -e "$test_work_dir" ] || fail "temporary test directory remains after cleanup"
if [ "$server_image_preexisting" -eq 0 ]; then
  docker image inspect "$server_image" >/dev/null 2>&1 && fail "test-pulled MinIO image remains after cleanup"
fi
if [ "$load_balancer_image_preexisting" -eq 0 ]; then
  docker image inspect "$load_balancer_image" >/dev/null 2>&1 && fail "test-pulled load-balancer image remains after cleanup"
fi
echo "PASS: containers, networks, volumes, temporary files, and test-pulled images were removed"
echo "RESULT: MinIO multi-site lifecycle test passed"
