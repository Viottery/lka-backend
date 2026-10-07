import ast
import inspect
import json
import sqlite3
from types import SimpleNamespace

from app.core import message_analysis
from app.core.background_jobs import BackgroundJobStore
from app.core.message_analysis import MessageAnalysisCoordinator
from app.domains.message_history import MessageHistoryService


class RecordingController:
    def __init__(self):
        self.reclassified = []

    def reclassify_task_usage(self, task_ids, pool):
        self.reclassified.append((task_ids, pool))


def test_both_message_analysis_paths_use_the_message_pool():
    tree = ast.parse(inspect.getsource(message_analysis))
    functions = {node.name: node for node in ast.walk(tree)
                 if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
    for name in ("_analyze", "_analyze_v3"):
        pools = [call.args[0].value for call in ast.walk(functions[name])
                 if isinstance(call, ast.Call) and isinstance(call.func, ast.Name)
                 and call.func.id == "workload_scope"]
        assert pools == ["background_message"]


def test_coordinator_migrates_only_durable_message_task_attributions(tmp_path):
    db_path = tmp_path / "messages.sqlite"
    jobs = BackgroundJobStore(db_path)
    service = MessageHistoryService(db_path, jobs)
    service.ensure_schema()
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO message_reading_families "
            "(family_id,conversation_key,start_seq,end_seq,max_tokens,max_calls) "
            "VALUES('family-with-opaque-id','conversation-a',1,2,1000,4)"
        )
        conn.execute(
            "INSERT INTO message_reading_manifests "
            "VALUES('completed-message-job','conversation-a',1,2,'{}','now')"
        )
        conn.execute(
            "INSERT INTO background_jobs "
            "(job_id,kind,scope_id,idempotency_key,payload_json,status,available_at,max_attempts,created_at,updated_at) "
            "VALUES('legacy-message-job','message_analysis','conversation-a','legacy',?,'succeeded','now',3,'now','now')",
            (json.dumps({"start_seq": 1}),),
        )
        conn.execute(
            "INSERT INTO background_jobs "
            "(job_id,kind,scope_id,idempotency_key,payload_json,status,available_at,max_attempts,created_at,updated_at) "
            "VALUES('memory-job','memory_extract','conversation-a','memory',?,'succeeded','now',3,'now','now')",
            (json.dumps({"start_seq": 1}),),
        )

    controller = RecordingController()
    llm_client = SimpleNamespace(workloads=controller)
    config = SimpleNamespace(model_dump=lambda **_: {}, worker_count=1)
    coordinator = MessageAnalysisCoordinator(
        service=service, store=jobs, config=config, llm_client=llm_client
    )

    assert controller.reclassified == [(
        ("completed-message-job", "family-with-opaque-id", "legacy-message-job"),
        "background_message",
    )]
    coordinator.stop()
