import importlib.util
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

from app.storage.sqlite_timing import TimingRecorder

SPEC = importlib.util.spec_from_file_location("runtime_metrics", Path(__file__).parents[1] / "scripts/runtime_metrics.py")
metrics = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(metrics)


def _real_summary(tmp_path, ts, phase="execute", database="main"):
    recorder = TimingRecorder(tmp_path / "timing.jsonl", sample_rate=1)
    for elapsed in (0.05, 0.2, 1.5, 20):
        recorder.record(database=database, phase=phase, operation="SELECT", statement_id="safe", elapsed_ms=elapsed,
                        error=None)
    recorder.last_summary -= 60
    recorder.flush_summary(force=True)
    recorder.handler.close(); recorder.closed = True
    row = json.loads((tmp_path / "timing.jsonl").read_text().splitlines()[-1])
    row["timestamp"] = ts.isoformat()
    return row


def test_interval_aggregation_and_duplicate_dedup(tmp_path):
    log = tmp_path / "logs"; log.mkdir()
    now = datetime.now(UTC); row = _real_summary(tmp_path, now - timedelta(seconds=10))
    (log / "database_timing.jsonl").write_text(json.dumps(row) + "\n" + json.dumps(row) + "\n")
    out = metrics.aggregate_logs(tmp_path, 300, now)
    group = out["sqlite"]["main"]["execute"]
    assert group["count"] == 4 and group["calls_per_second"] > 0
    assert group["p50_upper_ms"] == 0.5 and group["p95_upper_ms"] == 25
    assert "interval_seconds" in group
    assert out["coverage"]["deduplicated_records"] == 1
    assert group["bucket_bounds_ms"][-1] is None


def test_interval_rate_and_phase_buckets_are_independent(tmp_path):
    logs = tmp_path / "logs"; logs.mkdir()
    now = datetime.now(UTC)
    a = _real_summary(tmp_path, now - timedelta(seconds=5), phase="execute")
    b = _real_summary(tmp_path, now - timedelta(seconds=5), phase="acquire_write", database="other")
    (logs / "database_timing.jsonl").write_text("\n".join(map(json.dumps, (a, b))) + "\n")
    result = metrics.aggregate_logs(tmp_path, 300, now)["sqlite"]
    assert result["main"]["execute"]["count"] == 4
    assert result["other"]["acquire_write"]["count"] == 4
    assert result["main"]["execute"]["interval_seconds"] > 0
    assert result["other"]["acquire_write"]["interval_seconds"] > 0


def test_invalid_numeric_summary_is_classified_and_skipped(tmp_path):
    logs = tmp_path / "logs"; logs.mkdir()
    row = _real_summary(tmp_path, datetime.now(UTC))
    row["total_ms"] = float("nan")
    (logs / "database_timing.jsonl").write_text(json.dumps(row) + "\n")
    result = metrics.aggregate_logs(tmp_path, 300)
    assert result["sqlite"] == {}
    assert result["coverage"]["excluded_records"]["invalid_summary"] == 1


def test_truncated_and_missing_logs_are_reported(tmp_path):
    assert "timing_logs_missing" in metrics.aggregate_logs(tmp_path, 300)["errors"]
    log = tmp_path / "logs"; log.mkdir()
    p = log / "database_timing.jsonl"
    valid = _real_summary(tmp_path, datetime.now(UTC))
    p.write_bytes(b"x" * metrics.MAX_LOG_BYTES + b"\n" + (json.dumps(valid) + "\n").encode())
    out = metrics.aggregate_logs(tmp_path, 300)
    assert any("tail_truncated" in e for e in out["errors"])
    assert out["sqlite"]["main"]["execute"]["count"] == 4


@pytest.mark.parametrize("url", ["https://127.0.0.1/health", "http://example.com/health", "http://u:p@localhost/health", "http://localhost/a?q=1", "http://localhost/health#x", "http://localhost/other"])
def test_rejects_nonlocal_or_credentialed_health_url(url):
    with pytest.raises(ValueError): metrics.validate_health_url(url)


def test_accepts_loopback_health_url():
    assert metrics.validate_health_url("http://127.0.0.1:8765/health")


def test_database_state_is_read_only_and_missing_schema_unavailable(tmp_path):
    db = tmp_path / "existing.sqlite3"
    conn = sqlite3.connect(db); conn.execute("CREATE TABLE unrelated (id INTEGER)"); conn.commit(); conn.close()
    before = db.stat().st_size
    result = metrics.database_state(db)
    assert result["status"] == "unavailable"
    assert db.stat().st_size == before
    missing = tmp_path / "absent.sqlite3"
    assert metrics.database_state(missing)["status"] == "unavailable"
    assert not missing.exists()


def test_database_state_counts_inputs_and_closes_connection(tmp_path):
    db = tmp_path / "state.sqlite3"
    conn = sqlite3.connect(db)
    conn.executescript("""
      CREATE TABLE agent_runs(run_id TEXT,session_id TEXT,status TEXT,record_payload TEXT,created_at TEXT,updated_at TEXT);
      CREATE TABLE session_inputs(input_id TEXT,session_id TEXT,run_id TEXT,cancelled INTEGER);
      CREATE TABLE session_input_queues(session_id TEXT,paused INTEGER);
      INSERT INTO agent_runs VALUES('private-id','s','running','{}','','');
      INSERT INTO session_inputs VALUES('i1','s',NULL,0);
      INSERT INTO session_input_queues VALUES('s',1);
    """); conn.commit(); conn.close()
    real_connect = sqlite3.connect
    captured = []
    def connect(*args, **kwargs):
        result = real_connect(*args, **kwargs); captured.append(result); return result
    with patch.object(metrics.sqlite3, "connect", connect): result = metrics.database_state(db)
    assert result == {"status": "available", "runs_by_status": {"running": 1},
                      "pending_unbound_inputs": 1, "input_queues": {"queues": 1, "paused_queues": 1}}
    with pytest.raises(sqlite3.ProgrammingError): captured[0].execute("SELECT 1")


def test_output_cannot_be_inside_data_dir_even_via_symlink(tmp_path):
    data = tmp_path / "data"; data.mkdir()
    args = type("Args", (), {"data_dir": data, "window_seconds": 300, "output": data / "logs/out.json",
                              "database_state": False, "health_url": None})
    with pytest.raises(ValueError): metrics.sample(args)
    outside_link = tmp_path / "link.json"; outside_link.symlink_to(data / "lka.sqlite3")
    args.output = outside_link
    with pytest.raises(ValueError): metrics.sample(args)


def test_summary_rate_uses_complete_intervals_and_marks_overlap(tmp_path):
    logs = tmp_path / "logs"; logs.mkdir()
    now = datetime.now(UTC)
    row = _real_summary(tmp_path, now - timedelta(seconds=10))
    row["interval_seconds"] = 60
    earlier = {**row, "timestamp": (now - timedelta(seconds=70)).isoformat(), "max_ms": 40}
    partial = {**row, "timestamp": (now - timedelta(seconds=290)).isoformat()}
    http = {"kind": "http", "timestamp": now.isoformat(), "status": 200, "elapsed_ms": 10, "headers_ms": 1}
    path = logs / "database_timing.jsonl"
    path.write_text("\n".join(map(json.dumps, [row, earlier, partial, http])) + "\n")
    out = metrics.aggregate_logs(tmp_path, 300, now)
    group = out["sqlite"]["main"]["execute"]
    assert group["count"] == 8 and group["interval_seconds"] == 120
    assert group["calls_per_second"] == pytest.approx(8 / 120)
    assert group["max_ms"] == 40
    assert out["coverage"]["summary_intervals"] == 2
    assert out["coverage"]["excluded_records"]["outside_or_partial_window"] == 1
    assert out["http_sampled_records"]["requests"] == 1
    assert out["http_sampled_records"]["is_complete_request_count"] is False
    overlap = {**row, "timestamp": (now - timedelta(seconds=20)).isoformat()}
    with path.open("a") as stream: stream.write(json.dumps(overlap) + "\n")
    group = metrics.aggregate_logs(tmp_path, 300, now)["sqlite"]["main"]["execute"]
    assert group["calls_per_second"] is None and group["overlapping_intervals"] > 0


def test_corrupt_bounds_and_huge_numbers_do_not_break_snapshot(tmp_path):
    logs = tmp_path / "logs"; logs.mkdir()
    row = _real_summary(tmp_path, datetime.now(UTC))
    corrupt_bounds = {**row, "bucket_bounds_ms": [None, 25, None], "bucket_counts": [0, 4, 0]}
    huge = {**row, "total_ms": 10 ** 400}
    bad_http = {"kind": "http", "timestamp": row["timestamp"], "status": 200, "elapsed_ms": -1}
    (logs / "database_timing.jsonl").write_text("\n".join(map(json.dumps, [corrupt_bounds, huge, bad_http])) + "\n")
    out = metrics.aggregate_logs(tmp_path, 300)
    assert out["coverage"]["excluded_records"]["invalid_summary"] == 2
    assert out["coverage"]["excluded_records"]["invalid_http"] == 1
    assert "requests" not in out["http_sampled_records"]
    assert out["coverage"]["sqlite_summary_status"] == "unavailable"


def test_atomic_output_is_private_and_has_no_data_paths(tmp_path):
    import argparse
    import os
    data = tmp_path / "private-data"; data.mkdir()
    output = tmp_path / "metrics" / "latest.json"
    args = argparse.Namespace(data_dir=data, output=output, window_seconds=300, database_state=False, health_url=None)
    result = metrics.sample(args)
    metrics.atomic_json(output, result)
    assert json.loads(output.read_text())["storage"]["database_bytes"] is None
    assert str(data) not in output.read_text()
    if os.name != "nt": assert output.stat().st_mode & 0o777 == 0o600


def test_history_keeps_bounded_private_json_lines(tmp_path, monkeypatch):
    import os
    monkeypatch.setattr(metrics, "HISTORY_MAX_BYTES", 150)
    path = tmp_path / "metrics.jsonl"
    for i in range(30): metrics.append_history(path, {"sample": i, "status": "ok"})
    files = list(tmp_path.glob("metrics.jsonl*"))
    assert len(files) == 4
    assert json.loads(path.read_text().splitlines()[-1])["sample"] == 29
    for file in files:
        assert file.stat().st_size <= 150
        if os.name != "nt": assert file.stat().st_mode & 0o777 == 0o600


def test_history_path_cannot_target_data(tmp_path):
    import argparse
    data = tmp_path / "data"; data.mkdir()
    args = argparse.Namespace(data_dir=data, output=None, history=data / "overwrite.jsonl", window_seconds=300,
                              database_state=False, health_url=None)
    with pytest.raises(ValueError): metrics.sample(args)


@pytest.mark.parametrize("mode", ["healthy", "wrong_service", "redirect", "oversized"])
def test_health_checks_payload_and_never_follows_redirects(mode):
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append(self.path)
            self.send_response(302 if mode == "redirect" else 200)
            self.send_header("Location", "/must-not-be-requested")
            self.end_headers()
            payload = {"status": "ok", "service": "other" if mode == "wrong_service" else "local-knowledge-agent-os"}
            self.wfile.write(b"x" * 1025 if mode == "oversized" else json.dumps(payload).encode())

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=.02), daemon=True)
    thread.start()
    try:
        result = metrics.health(f"http://127.0.0.1:{server.server_port}/health")
        assert result["status"] == ("available" if mode == "healthy" else "unavailable")
        assert requests == ["/health"]
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=2)
