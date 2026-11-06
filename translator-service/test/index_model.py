"""Validate the installed 9B through real owner Kafka/SQL, then clean own probes."""

from __future__ import annotations

import argparse
import json
import subprocess
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

ROOT = Path(__file__).resolve().parents[1]
COMPOSE = ["docker", "compose", "-f", "compose.remote.yml", "--env-file", ".env"]
ROLES = [
    "translator-worker",
    "translator-outbox",
    "translator-normal-consumer",
    "translator-priority-consumer",
]

# Executed only inside the existing API or the owner's one-shot admin role.
PROBE = r"""
import json,sys,time,uuid
from pathlib import Path
from sqlalchemy import text
from confluent_kafka import Consumer,Producer,TopicPartition
from translator_service.contracts import (
 digest,parse,NORMAL_REQUEST_TOPIC_V2,PRIORITY_REQUEST_TOPIC_V2,RESULT_TOPIC_V2
)
from translator_service.runtime import repository,kafka_configuration
from translator_service.operator_translation import TranslationInput,request_bytes
action,args=sys.argv[1],json.loads(sys.argv[2])
topics=[NORMAL_REQUEST_TOPIC_V2,PRIORITY_REQUEST_TOPIC_V2,RESULT_TOPIC_V2]
lock=json.loads(Path('translator.lock.json').read_text())
def offsets():
 c=Consumer({**kafka_configuration(),'group.id':'translator-model-validation','enable.auto.commit':False})
 try:
  metadata=c.list_topics(timeout=15)
  result={}
  for name in topics:
   result[name]=[]
   for part in metadata.topics[name].partitions:
    lo,hi=c.get_watermark_offsets(TopicPartition(name,part),timeout=15)
    result[name].append({'partition':part,'low':lo,'high':hi})
  return result
 finally: c.close()
def publish(raw,queue):
 p=Producer({**kafka_configuration(),'enable.idempotence':True,'acks':'all','message.timeout.ms':10000})
 ack=[]
 topic=PRIORITY_REQUEST_TOPIC_V2 if queue=='priority' else NORMAL_REQUEST_TOPIC_V2
 identity=parse(raw,'TranslationRequestedV2')['data']['request_id']
 p.produce(topic,key=identity.encode(),value=raw,
           on_delivery=lambda err,msg:ack.append(err))
 assert p.flush(15)==0 and ack==[None]
if action=='offsets':
 print(json.dumps(offsets()));sys.exit()
if action=='trim':
 from confluent_kafka.admin import AdminClient
 assert offsets()==args['offsets'], 'KAFKA_CHANGED_BEFORE_CLEANUP'
 # Count alone cannot prove ownership: validate every immutable test record.
 c=Consumer({**kafka_configuration(),'group.id':'translator-model-validation',
             'enable.auto.commit':False})
 try:
  for name,items in args['offsets'].items():
   for item in items:
    next_offset=item['low']
    if next_offset==item['high']: continue
    c.assign([TopicPartition(name,item['partition'],next_offset)])
    deadline=time.monotonic()+30
    while next_offset<item['high']:
     if time.monotonic()>=deadline: raise RuntimeError('RECORD_READ_TIMEOUT')
     message=c.poll(1)
     if message is None: continue
     assert not message.error(), 'KAFKA_RECORD_READ_FAILED'
     raw=message.value()
     kind=('TranslationCompletedV2' if name==RESULT_TOPIC_V2
           else 'TranslationRequestedV2')
     data=parse(raw,kind)['data']
     identity=data['request_id']
     assert identity in args['digests'], 'FOREIGN_KAFKA_RECORD'
     bound=data['request_digest'] if name==RESULT_TOPIC_V2 else digest(raw)
     assert bound==args['digests'][identity], 'KAFKA_RECORD_DIGEST_MISMATCH'
     next_offset=message.offset()+1
 finally: c.close()
 assert offsets()==args['offsets'], 'KAFKA_CHANGED_BEFORE_CLEANUP'
 admin=AdminClient(kafka_configuration())
 records=[TopicPartition(name,item['partition'],item['high'])
          for name,items in args['offsets'].items() for item in items]
 futures=admin.delete_records(records)
 for future in futures.values(): future.result(timeout=30)
 print(json.dumps({'trimmed':True}));sys.exit()
repo=repository()
try:
 if action=='baseline':
  with repo.engine.connect() as c:
   print(json.dumps({
    'translator_ref':lock['translator_ref'],
    'jobs':c.execute(text('SELECT count(*) FROM translation_job')).scalar(),
    'outbox':c.execute(text('SELECT count(*) FROM translation_outbox')).scalar(),
    'priority_streak':c.execute(text(
      'SELECT priority_streak FROM translation_control')).scalar(),
    'offsets':offsets()}))
 elif action=='send':
  payload=TranslationInput(**args['payload'])
  raw=request_bytes(payload,'sha256:'+('b'*64),args['id'])
  publish(raw,payload.queue)
  print(json.dumps({'request_digest':digest(raw)}))
 elif action in ('state','repeat'):
  with repo.engine.connect() as c:
   row=c.execute(text(
    'SELECT j.payload,j.state,j.priority,j.attempt_count,j.request_digest,'
    'o.state AS outbox_state,o.payload AS result FROM translation_job j '
    'LEFT JOIN translation_outbox o USING(request_id) WHERE j.request_id=:id'
   ),{'id':args['id']}).mappings().first()
   if row is None: print('{}')
   elif action=='repeat':
    assert row['request_digest']==args['digest'] and row['state']=='SUCCEEDED'
    publish(bytes(row['payload']),args['queue']);print(json.dumps({'repeated':True}))
   else:
    result=dict(row);result.pop('payload')
    result['result']=(parse(bytes(row['result']),'TranslationCompletedV2')['data']
                      if row['result'] else None)
    print(json.dumps(result,ensure_ascii=False))
 elif action=='clean':
  with repo.engine.begin() as c:
   ids={'ids':list(args['digests'])}
   rows=c.execute(text('SELECT request_id,request_digest FROM translation_job '
                       'WHERE request_id=ANY(:ids)'),ids).mappings().all()
   assert all(args['digests'][r['request_id']]==r['request_digest'] for r in rows)
   c.execute(text('DELETE FROM translation_outbox WHERE request_id=ANY(:ids)'),ids)
   c.execute(text('DELETE FROM translation_job WHERE request_id=ANY(:ids)'),ids)
   c.execute(text('UPDATE translation_control SET priority_streak=:streak'),
             {'streak':args['streak']})
  print(json.dumps({'removed_jobs':len(rows)}))
finally: repo.engine.dispose()
"""


def command(arguments: list[str], timeout: int = 120) -> str:
    value = subprocess.run(
        arguments, cwd=ROOT, text=True, capture_output=True, timeout=timeout
    )
    if value.returncode:
        # Never copy subprocess output containing env, credentials or signed URLs.
        raise RuntimeError(f"command failed: {arguments[:3]}, exit {value.returncode}")
    return value.stdout.strip()


def probe(action: str, args: dict[str, Any] | None = None, admin: bool = False) -> Any:
    role = (
        [
            "run",
            "--rm",
            "--no-deps",
            "--entrypoint",
            "python",
            "translator-topic-provisioner",
        ]
        if admin
        else ["exec", "-T", "translator-api", "python"]
    )
    return json.loads(
        command(COMPOSE + role + ["-c", PROBE, action, json.dumps(args or {})])
    )


def wait_result(identity: str, state: str = "SUCCEEDED") -> dict[str, Any]:
    deadline = time.monotonic() + 360
    while time.monotonic() < deadline:
        value = probe("state", {"id": identity})
        if (
            value.get("state") in {"FAILED", "SUCCEEDED"}
            and value.get("outbox_state") == "PUBLISHED"
        ):
            assert value["state"] == state
            return cast(dict[str, Any], value)
        time.sleep(1)
    raise TimeoutError("MODEL_TEST_RESULT_TIMEOUT")


def healthy_model() -> None:
    deadline = time.monotonic() + 240
    while time.monotonic() < deadline:
        identity = command(COMPOSE + ["ps", "-q", "translator-model"])
        if (
            identity
            and command(
                ["docker", "inspect", "--format", "{{.State.Health.Status}}", identity]
            )
            == "healthy"
        ):
            return
        time.sleep(2)
    raise TimeoutError("MODEL_TEST_START_TIMEOUT")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--allow-restart-and-clean", action="store_true")
    args = parser.parse_args()
    if not args.allow_restart_and_clean:
        parser.error("explicit local restart and owned-probe cleanup required")
    cfg = dict(
        line.split("=", 1)
        for line in (ROOT / ".env").read_text().splitlines()
        if line.startswith("AELEV_")
    )
    assert cfg["AELEV_INFRASTRUCTURE_TRANSLATOR_ENVIRONMENT"] == "local"
    assert cfg["AELEV_INFRASTRUCTURE_TRANSLATOR_KAFKA_SECURITY_PROTOCOL"] == "PLAINTEXT"
    assert cfg["AELEV_INFRASTRUCTURE_TRANSLATOR_DATABASE_HOST"] == "translator-database"
    stages: list[str] = []
    results: list[dict[str, Any]] = []
    digests: dict[str, str] = {}
    baseline: dict[str, Any] = {}
    cleanup = False
    passed = False
    failure = ""
    prepared = False
    counts = {
        "translation.requests.v2": 0,
        "translation.requests.priority.v2": 0,
        "translation.results.v2": 0,
    }

    def check(label: str) -> None:
        stages.append(label)
        print("PASS: " + label, flush=True)

    def send(queue: str, value: str, source: str = "en", target: str = "de") -> str:
        identity = str(uuid.uuid4())
        response = probe(
            "send",
            {
                "id": identity,
                "payload": {
                    "queue": queue,
                    "source_language": source,
                    "target_language": target,
                    "text": value,
                },
            },
        )
        digests[identity] = response["request_digest"]
        counts[
            (
                "translation.requests.priority.v2"
                if queue == "priority"
                else "translation.requests.v2"
            )
        ] += 1
        counts["translation.results.v2"] += 1
        return identity

    try:
        baseline = probe("baseline")
        assert baseline["translator_ref"].startswith(
            "IndexTeam/Index-Translate-9B-q4km@sha256:"
        )
        assert baseline["jobs"] == baseline["outbox"] == 0
        assert all(
            item["low"] == item["high"]
            for parts in baseline["offsets"].values()
            for item in parts
        )
        healthy_model()
        assert set(ROLES + ["translator-model", "translator-api"]) <= set(
            command(COMPOSE + ["ps", "--status", "running", "--services"]).splitlines()
        )
        prepared = True
        check(
            "Настоящие Kafka/БД пусты, установленная 9B healthy; "
            "отдельные компоненты не создаются"
        )
        identity = send("normal", "The climate observatory did not revise the temperature estimate.")
        normal = wait_result(identity)
        assert normal["priority"] == 100 and "translator_ref" not in normal["result"]
        assert "Deutschland" not in normal["result"]["translated_text"]
        results.append(normal)
        check("Обычная очередь: полный путь Kafka → БД → 9B → outbox ACK")
        probe(
            "repeat", {"id": identity, "digest": digests[identity], "queue": "normal"}
        )
        counts["translation.requests.v2"] += 1
        counts["translation.results.v2"] += 1
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            parts = probe("offsets")["translation.results.v2"]
            if sum(part["high"] - part["low"] for part in parts) >= 2:
                break
            time.sleep(1)
        else:
            raise TimeoutError("DUPLICATE_RESULT_NOT_REPUBLISHED")
        repeated = wait_result(identity)
        assert (
            repeated["attempt_count"] == normal["attempt_count"]
            and repeated["result"] == normal["result"]
        )
        check("Повтор точных байтов: тот же результат без нового вычисления")
        urgent = wait_result(
            send("priority", "Замок на вершине холма принадлежал семье.", "ru", "en")
        )
        assert (
            urgent["priority"] == 0
            and "castle" in urgent["result"]["translated_text"].lower()
        )
        results.append(urgent)
        check("Приоритетная очередь и явный исходный язык: castle, не lock")
        body = (
            "The report records 42 confirmed events; "
            "uncertain observations remain uncertain. " * 12
        ) + "Final reference: FINAL_MARKER_9B."
        long = wait_result(send("normal", body, target="ru"))
        assert "FINAL_MARKER_9B" in long["result"]["translated_text"]
        results.append(long)
        check("Текст длиннее одного фрагмента: финальный маркер сохранён")
        unsupported = wait_result(
            send("normal", "unsupported language probe", source="zz"), "FAILED"
        )
        assert (
            unsupported["result"]["error_code"]
            == "TRANSLATOR_UNSUPPORTED_SOURCE_LANGUAGE"
        )
        check("Неподдерживаемый язык: FAILED, не ошибочный успешный перевод")
        command(COMPOSE + ["stop", "--timeout", "30", "translator-model"])
        pending = send("priority", "The service recovered after restart.", target="fr")
        time.sleep(3)
        assert probe("state", {"id": pending}).get("state") not in {
            "SUCCEEDED",
            "FAILED",
        }
        command(COMPOSE + ["start", "translator-model"])
        healthy_model()
        results.append(wait_result(pending))
        check("Остановка модели не теряет задание; после запуска перевод завершён")
        passed = True
    except Exception as error:
        failure = type(error).__name__
    finally:
        try:
            if prepared:
                command(COMPOSE + ["start", "translator-model"])
            if digests:
                command(COMPOSE + ["stop", "--timeout", "250"] + ROLES, timeout=300)
                end = probe("offsets")
                for name, parts in end.items():
                    assert (
                        sum(item["high"] - item["low"] for item in parts)
                        <= counts[name]
                    ), "UNEXPECTED_KAFKA_DATA"
                probe("trim", {"offsets": end, "digests": digests}, admin=True)
                removed = probe(
                    "clean", {"digests": digests, "streak": baseline["priority_streak"]}
                )
                assert removed["removed_jobs"] <= len(digests)
                after = probe("baseline")
                assert after["jobs"] == after["outbox"] == 0
                assert all(
                    item["low"] == item["high"]
                    for parts in after["offsets"].values()
                    for item in parts
                )
                check(
                    "Удалены только созданные тестом записи; "
                    "пустые topics и прежние ресурсы сохранены"
                )
            cleanup = True
        except Exception as error:
            passed = False
            failure += "; cleanup=" + type(error).__name__
        finally:
            if prepared and digests:
                command(COMPOSE + ["start"] + ROLES)
        verdict = "PASS" if passed and cleanup else "FAIL"
        report = {
            "status": verdict,
            "at": datetime.now(UTC).isoformat(),
            "translator_ref": baseline.get("translator_ref"),
            "stages": stages,
            "translations": results,
            "cleanup": cleanup,
            "failure": failure,
        }
        (ROOT / "test/index-model-result.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n"
        )
        (ROOT / "test/index-model-result.md").write_text(
            "# translator-service — установленная Index-Translate 9B\n\n"
            + f"Дата UTC: {report['at']}. Итог: {verdict}.\n\n"
            + "\n".join("- PASS: " + stage for stage in stages)
            + f"\n\nОчистка: {cleanup}. Ошибка: {failure or 'нет'}.\n\n"
            + "Реальные компоненты владельца, без подмены ответов. "
            "После проверки собственные роли запущены; чужие "
            "контейнеры/БД/topics не управляются. Проверка не удостоверяет "
            "все языки, географическую задержку, production PKI или смысловую "
            "точность произвольного текста. Учебные ответы — в соседнем JSON.\n"
        )
        print(verdict + ": test/index-model-result.md", flush=True)
    return 0 if passed and cleanup else 1


if __name__ == "__main__":
    raise SystemExit(main())
