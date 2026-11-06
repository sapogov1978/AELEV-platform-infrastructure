#!/usr/bin/env bash
set -Eeuo pipefail

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
kafka_dir=$(cd -- "${script_dir}/.." && pwd)
run_id="$(date -u +%Y%m%d%H%M%S)-$$"
topic="aelev.infrastructure.kafka.lifecycle-test"
first_message="before-restart-${run_id}"
second_message="after-restart-${run_id}"

export AELEV_INFRASTRUCTURE_KAFKA_COMPOSE_PROJECT="aelev-kafka-test-${run_id}"
export AELEV_INFRASTRUCTURE_KAFKA_BIND="127.0.0.1"
export AELEV_INFRASTRUCTURE_KAFKA_PORT="19092"
export AELEV_INFRASTRUCTURE_KAFKA_ADVERTISED_LISTENERS="PLAINTEXT://kafka:9092"
export AELEV_INFRASTRUCTURE_KAFKA_HEAP_OPTS="-Xms256m -Xmx512m"
export AELEV_INFRASTRUCTURE_KAFKA_MESSAGE_MAX_BYTES="8388608"
export AELEV_INFRASTRUCTURE_KAFKA_DATA_VOLUME="${AELEV_INFRASTRUCTURE_KAFKA_COMPOSE_PROJECT}-data"
export AELEV_INFRASTRUCTURE_KAFKA_NETWORK="${AELEV_INFRASTRUCTURE_KAFKA_COMPOSE_PROJECT}-network"
export AELEV_INFRASTRUCTURE_KAFKA_UI_STATE_VOLUME="${AELEV_INFRASTRUCTURE_KAFKA_COMPOSE_PROJECT}-status"
export AELEV_INFRASTRUCTURE_KAFKA_UI_EGRESS_NETWORK="${AELEV_INFRASTRUCTURE_KAFKA_COMPOSE_PROJECT}-status-egress"

compose=(
  docker compose
  --env-file "${kafka_dir}/.env.example"
  -f "${kafka_dir}/compose.yml"
  -f "${script_dir}/compose.test.yml"
)
image_ref=$("${compose[@]}" config --images)
[[ -n "${image_ref}" && "${image_ref}" != *$'\n'* ]]

image_preexisting=false
if docker image inspect "${image_ref}" >/dev/null 2>&1; then
  image_preexisting=true
fi

cleanup_complete=false
cleanup_resources() {
  local cleanup_status=0
  "${compose[@]}" stop kafka >/dev/null 2>&1 || cleanup_status=1
  "${compose[@]}" down --volumes --remove-orphans >/dev/null 2>&1 || cleanup_status=1
  if [[ "${image_preexisting}" == false ]]; then
    docker image rm "${image_ref}" >/dev/null 2>&1 || cleanup_status=1
  fi
  return "${cleanup_status}"
}

on_exit() {
  local test_status=$?
  trap - EXIT INT TERM
  if [[ "${cleanup_complete}" == false ]]; then
    cleanup_resources || true
  fi
  if (( test_status != 0 )); then
    printf 'FAIL project=%s cleanup=attempted\n' "${AELEV_INFRASTRUCTURE_KAFKA_COMPOSE_PROJECT}" >&2
  fi
  exit "${test_status}"
}
trap on_exit EXIT INT TERM

wait_for_health() {
  local container_id=""
  local health=""
  local attempt
  for attempt in {1..90}; do
    container_id=$("${compose[@]}" ps -q kafka)
    if [[ -n "${container_id}" ]]; then
      health=$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}missing{{end}}' "${container_id}")
      if [[ "${health}" == healthy ]]; then
        return 0
      fi
    fi
    sleep 2
  done
  "${compose[@]}" logs --no-color kafka >&2 || true
  printf 'Kafka did not become healthy after %s attempts; last status=%s\n' "${attempt}" "${health:-missing}" >&2
  return 1
}

assert_message_count() {
  local expected_count=$1
  local output
  output=$("${compose[@]}" exec -T kafka /opt/kafka/bin/kafka-console-consumer.sh \
    --bootstrap-server localhost:9092 \
    --topic "${topic}" \
    --from-beginning \
    --max-messages "${expected_count}" \
    --timeout-ms 15000 2>/dev/null)
  grep -Fqx "${first_message}" <<<"${output}"
  if (( expected_count == 2 )); then
    grep -Fqx "${second_message}" <<<"${output}"
  fi
}

"${compose[@]}" config --quiet
"${compose[@]}" up -d --pull missing kafka
wait_for_health

container_id=$("${compose[@]}" ps -q kafka)
[[ -n "${container_id}" ]]

"${compose[@]}" exec -T kafka /opt/kafka/bin/kafka-topics.sh \
  --bootstrap-server localhost:9092 \
  --create \
  --if-not-exists \
  --topic "${topic}" \
  --partitions 1 \
  --replication-factor 1 >/dev/null

printf '%s\n' "${first_message}" | "${compose[@]}" exec -T kafka \
  /opt/kafka/bin/kafka-console-producer.sh \
  --bootstrap-server localhost:9092 \
  --topic "${topic}" >/dev/null
assert_message_count 1

"${compose[@]}" stop kafka >/dev/null
[[ "$(docker inspect --format '{{.State.Running}}' "${container_id}")" == false ]]

"${compose[@]}" up -d --no-recreate kafka
wait_for_health
assert_message_count 1

printf '%s\n' "${second_message}" | "${compose[@]}" exec -T kafka \
  /opt/kafka/bin/kafka-console-producer.sh \
  --bootstrap-server localhost:9092 \
  --topic "${topic}" >/dev/null
assert_message_count 2

"${compose[@]}" stop kafka >/dev/null
cleanup_resources
cleanup_complete=true

if docker container inspect "${container_id}" >/dev/null 2>&1; then
  printf 'Test container was not removed: %s\n' "${container_id}" >&2
  exit 1
fi
if docker volume inspect "${AELEV_INFRASTRUCTURE_KAFKA_DATA_VOLUME}" >/dev/null 2>&1; then
  printf 'Test volume was not removed: %s\n' "${AELEV_INFRASTRUCTURE_KAFKA_DATA_VOLUME}" >&2
  exit 1
fi
if docker network inspect "${AELEV_INFRASTRUCTURE_KAFKA_NETWORK}" >/dev/null 2>&1; then
  printf 'Test network was not removed: %s\n' "${AELEV_INFRASTRUCTURE_KAFKA_NETWORK}" >&2
  exit 1
fi
if [[ -n "$(docker container ls -aq --filter "label=com.docker.compose.project=${AELEV_INFRASTRUCTURE_KAFKA_COMPOSE_PROJECT}")" ]]; then
  printf 'Containers with the test project label remain\n' >&2
  exit 1
fi
if [[ "${image_preexisting}" == false ]] && docker image inspect "${image_ref}" >/dev/null 2>&1; then
  printf 'Image pulled only for the test was not removed: %s\n' "${image_ref}" >&2
  exit 1
fi

printf 'PASS project=%s\n' "${AELEV_INFRASTRUCTURE_KAFKA_COMPOSE_PROJECT}"
printf 'availability_before_restart=healthy\n'
printf 'message_before_restart=verified\n'
printf 'container_stopped=verified\n'
printf 'availability_after_restart=healthy\n'
printf 'persisted_message_after_restart=verified\n'
printf 'new_message_after_restart=verified\n'
printf 'cleanup_container=removed\n'
printf 'cleanup_volume=removed\n'
printf 'cleanup_network=removed\n'
if [[ "${image_preexisting}" == true ]]; then
  printf 'cleanup_image=preserved_preexisting\n'
else
  printf 'cleanup_image=removed_test_pull\n'
fi
