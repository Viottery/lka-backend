from __future__ import annotations

import io
import threading
import urllib.error
from collections import Counter

import pytest

from scripts.stress_mysql_http import (
    LabHttpClient,
    LabInputError,
    RequestBook,
    StressRunner,
    bounded_plan,
    parse_base_url,
    percentile,
    summarize_live_sse,
    summarize_sse,
)


def test_loopback_url_accepts_localhost_and_loopback_ip_only():
    assert parse_base_url("http://127.0.0.1:8768").hostname == "127.0.0.1"
    assert parse_base_url("http://localhost:8768").hostname == "localhost"
    assert parse_base_url("http://[::1]:8768").hostname == "::1"
    for url in (
        "https://127.0.0.1:8768",
        "http://example.com:8768",
        "http://127.0.0.1:8768/path",
        "http://user:password@127.0.0.1:8768",
    ):
        with pytest.raises(LabInputError):
            parse_base_url(url)


def test_plan_enforces_concurrency_and_total_request_bounds():
    plan = bounded_plan(24, 8, 16, 120)
    assert plan["total_turns"] == 96
    assert plan["estimated_http_requests_upper_bound"] <= 2000
    assert plan["input_attempts"] == 70
    assert plan["unique_input_count"] == 64
    assert plan["live_sse_streams"] == 16
    with pytest.raises(LabInputError):
        bounded_plan(93, 32, 16, 120)
    with pytest.raises(LabInputError):
        bounded_plan(1, 33, 16, 120)
    with pytest.raises(LabInputError):
        bounded_plan(1, 8, 17, 120)
    with pytest.raises(LabInputError):
        bounded_plan(1, 8, 16, 121)


def test_percentiles_use_nearest_rank_and_empty_is_null():
    assert percentile([9, 1, 3, 5], .50) == 3
    assert percentile([9, 1, 3, 5], .95) == 9
    assert percentile([], .95) is None


def test_sse_summary_reports_events_without_retaining_payloads():
    body = (b"id: run:1\nevent: run_started\ndata: {\"secret\":\"do not report\"}\n\n"
            b"id: run:2\nevent: run_completed\ndata: {}\n\n")
    summary = summarize_sse(body)
    assert summary == {"event_count": 2, "failed_event": False}
    failed = summarize_sse(b"event: run_failed\ndata: {}\n\n")
    assert failed["failed_event"] is True


def test_live_sse_summary_extracts_only_run_and_terminal_contract():
    body = (b"event: run_started\ndata: {\"run_id\":\"synthetic-run\",\"secret\":\"hidden\"}\n\n"
            b"event: run_completed\ndata: {\"run_id\":\"synthetic-run\"}\n\n")
    assert summarize_live_sse(body) == {
        "event_count": 2, "failed_event": False, "run_id": "synthetic-run", "terminal_event": True,
    }


def test_input_burst_has_64_unique_payloads_and_six_exact_retries():
    runner = object.__new__(StressRunner)
    runner.prefix = "stress_unit"
    attempts = runner._input_attempts()
    assert len(attempts) == 70
    by_client_id = {}
    for item in attempts:
        prior = by_client_id.setdefault(item["client_message_id"], item)
        assert prior == item
    assert len(by_client_id) == 64
    counts = Counter(item["client_message_id"] for item in attempts)
    assert sum(count - 1 for count in counts.values()) == 6


def test_mixed_levels_fit_history_and_turn_barrier_and_read_session_contract(monkeypatch):
    class Client:
        timeout = .5

        def health(self, *_):
            return True

        def request(self, method, path, book, operation, body=None, **kwargs):
            book.add(operation, 200, .1)
            if method == "POST" and path == "/sessions":
                return 200, {"session": {"session_id": "synthetic-session"}, "messages": []}
            return 200, {"messages": []}

    runner = StressRunner(Client(), 2, 8, 1, 1)
    runner._history_seed_session = "history-session"
    runner.run_ids = ["seed-run"]
    monkeypatch.setattr(runner, "run_turn", lambda *a, **k: (200, "synthetic-run", {}))
    monkeypatch.setattr(runner, "verify_run", lambda *a, **k: True)
    results = runner.run_levels(RequestBook("setup"))
    assert [row["completed_turns"] for row in results] == [2, 2, 2, 2]
    assert all(not row["invariant_failures"] for row in results)
    assert all(row["by_operation"]["mixed_history_get"]["requests"] for row in results)


class _CapacityClient:
    timeout = .5

    def __init__(self, refusal_operation):
        self.refusal_operation = refusal_operation
        self.refusal_sent = False
        self.session_count = 0
        self.run_count = 0

    def health(self, *_):
        return True

    def request(self, method, path, book, operation, body=None, **kwargs):
        refuse = operation == self.refusal_operation and not self.refusal_sent
        if refuse:
            self.refusal_sent = True
        book.add(operation, 503 if refuse else 200, .1)
        if method == "POST" and path == "/sessions":
            self.session_count += 1
            return 200, {"session": {"session_id": f"synthetic-session-{self.session_count}"}}
        if refuse:
            return 503, {
                "detail": "agent_execution_capacity_full",
                "accepted": False,
                "retry_safe": True,
            }
        if operation in {"turn_post", "capacity_recovery_post"}:
            self.run_count += 1
            return 200, {"run_id": f"synthetic-run-{self.run_count}"}
        if operation == "live_sse_post":
            self.run_count += 1
            return 200, {
                "run_id": f"synthetic-run-{self.run_count}",
                "terminal_event": True,
                "failed_event": False,
                "event_count": 2,
            }
        if "client_request_id=" in path:
            return 200, {"runs": []}
        if path.endswith("/events"):
            return 200, {"events": [{"type": "run_completed"}]}
        if path.startswith("/agent/runs/"):
            return 200, {"status": "completed"}
        return 200, {"messages": [{"content": "synthetic"}]}


def test_expected_http_error_preserves_capacity_refusal_contract():
    client = LabHttpClient(parse_base_url("http://127.0.0.1:8768"), timeout=.5)

    class ErrorOpener:
        def open(self, request, timeout):
            raise urllib.error.HTTPError(
                request.full_url, 503, "capacity", {},
                io.BytesIO(b'{"detail":"agent_execution_capacity_full","accepted":false,"retry_safe":true}'),
            )

    client._opener = ErrorOpener()
    status, body = client.request(
        "POST", "/agent/turn", RequestBook("http_error"), "turn_post",
        {"session_id": "synthetic", "client_request_id": "synthetic"}, expected={200, 503},
    )

    assert status == 503
    assert StressRunner._is_capacity_refusal(status, body)
    assert not StressRunner._is_capacity_refusal(
        status, {"detail": "agent_execution_capacity_full", "accepted": True, "retry_safe": True},
    )


def test_levels_count_capacity_refusals_and_verify_fresh_probe_recovers():
    runner = StressRunner(_CapacityClient("turn_post"), 1, 2, 1, 1)
    runner._history_seed_session = "history-session"
    runner.run_ids = ["seed-run"]

    results = runner.run_levels(RequestBook("setup"))

    first = results[0]
    assert first["requested_turns"] == 1
    assert first["accepted_turns"] == 0
    assert first["completed_turns"] == 0
    assert first["refused_turns"] == 1
    assert first["recovery_probe_passed"] is True, first["invariant_failures"]
    assert not first["invariant_failures"]


def test_live_sse_counts_capacity_refusals_separately_from_completed_streams():
    runner = StressRunner(_CapacityClient("live_sse_post"), 1, 2, 1, 1)

    result = runner.run_live_sse(RequestBook("setup"))

    assert result["requested_streams"] == 16
    assert result["accepted_streams"] == 15
    assert result["completed_streams"] == 15, result["invariant_failures"]
    assert result["refused_streams"] == 1
    assert result["recovery_probe_passed"] is True
    assert not result["invariant_failures"]


@pytest.mark.parametrize("refusal_status", [409, 503])
def test_same_session_contention_accepts_only_conflict_or_structured_capacity_refusal(refusal_status):
    class SameSessionClient(_CapacityClient):
        def __init__(self):
            super().__init__("unused")
            self.contenders = 0
            self.lock = threading.Lock()

        def request(self, method, path, book, operation, body=None, **kwargs):
            if operation == "same_session_turn":
                with self.lock:
                    self.contenders += 1
                    index = self.contenders
                if index == 1:
                    self.run_count += 1
                    book.add(operation, 200, .1)
                    return 200, {"run_id": "same-session-accepted"}
                if refusal_status == 409:
                    book.add(operation, 409, .1)
                    return 409, {"detail": "session_execution_conflict"}
                book.add(operation, 503, .1)
                return 503, {
                    "detail": "agent_execution_capacity_full",
                    "accepted": False,
                    "retry_safe": True,
                }
            if operation == "same_session_runs_get":
                book.add(operation, 200, .1)
                return 200, {"runs": [{"run_id": "same-session-accepted"}]}
            if operation == "same_session_run_get":
                book.add(operation, 200, .1)
                return 200, {
                    "status": "completed",
                    "started_at": "2026-10-29T00:00:00+00:00",
                    "completed_at": "2026-10-29T00:00:01+00:00",
                }
            return super().request(method, path, book, operation, body, **kwargs)

    runner = StressRunner(SameSessionClient(), 1, 2, 1, 1)

    result = runner.run_same_session(RequestBook("setup"))

    assert result["requested_requests"] == 2
    assert result["accepted_requests"] == 1
    assert result["completed_requests"] == 1
    assert result["refused_requests"] == 1
    assert result["expected_409_conflicts"] == int(refusal_status == 409)
    assert result["recovery_probe_passed"] is True
    assert not result["invariant_failures"]
