"""Exercise a loopback-only isolated LKA HTTP service with synthetic Agent turns.

No request is retried. Use only against a disposable mock-model API and isolated
MySQL schema. Reports contain aggregate timings/statuses, never request payloads,
credentials, run IDs, session IDs, or response bodies.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import http.client
import ipaddress
import json
import math
import secrets
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from datetime import UTC, datetime
from http.cookies import SimpleCookie
from pathlib import Path
from typing import Any

_MAX_CONCURRENCY = 32
_MAX_HTTP_REQUESTS = 2000
_MAX_RESPONSE_BYTES = 2_000_000


class LabInputError(ValueError):
    pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def parse_base_url(value: str) -> urllib.parse.SplitResult:
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme != "http" or not parsed.hostname or parsed.username or parsed.password:
        raise LabInputError("base URL must be unauthenticated http:// loopback")
    if parsed.query or parsed.fragment or parsed.path not in {"", "/"}:
        raise LabInputError("base URL must not contain a path, query, or fragment")
    host = parsed.hostname.lower()
    if host != "localhost":
        try:
            if not ipaddress.ip_address(host).is_loopback:
                raise LabInputError("base URL must resolve to loopback")
        except ValueError as exc:
            raise LabInputError("base URL must resolve to loopback") from exc
    try:
        if parsed.port is not None and not 1 <= parsed.port <= 65535:
            raise LabInputError("base URL port is invalid")
    except ValueError as exc:
        raise LabInputError("base URL port is invalid") from exc
    return parsed


def bounded_plan(turns_per_level: int, same_session_requests: int, sse_requests: int,
                 input_drain_timeout: float) -> dict[str, int]:
    if type(turns_per_level) is not int or not 1 <= turns_per_level <= 64:
        raise LabInputError("turns-per-level must be between 1 and 64")
    if type(same_session_requests) is not int or not 2 <= same_session_requests <= _MAX_CONCURRENCY:
        raise LabInputError("same-session requests must be between 2 and 32")
    if type(sse_requests) is not int or not 1 <= sse_requests <= 16:
        raise LabInputError("SSE requests must be between 1 and 16")
    if not math.isfinite(input_drain_timeout) or not 1 <= input_drain_timeout <= 120:
        raise LabInputError("input drain timeout must be between 1 and 120 seconds")
    turns = turns_per_level * 4
    # Each complete turn has session creation, POST, run/event verification and
    # history read. One concurrent history read per turn batch is added for the
    # mixed workload; also reserve room for health/auth/idempotency/contention.
    batches = sum(math.ceil(turns_per_level / workers) for workers in (1, 4, 8, 16))
    # Durable queue burst: 64 unique inputs plus six exact retries, one manager create, bounded
    # 500ms polling for at most 120s, one final history read and health checks.
    drain_polls = math.ceil(input_drain_timeout / .5) + 1
    estimated = (turns * 5 + batches + 5 * same_session_requests + sse_requests
                 + 70 + drain_polls + 50 + 16 * 6
                 # One persisted-run absence lookup per possible capacity refusal,
                 # plus at most six distinct recovery probes (POST + 3 reads each).
                 + turns + 16 + same_session_requests + 6 * 4)
    if estimated > _MAX_HTTP_REQUESTS:
        raise LabInputError(f"planned request count exceeds {_MAX_HTTP_REQUESTS}")
    return {"turns_per_level": turns_per_level, "total_turns": turns,
            "same_session_requests": same_session_requests, "sse_requests": sse_requests,
            "input_attempts": 70, "unique_input_count": 64,
            "live_sse_streams": 16,
            "input_drain_timeout_seconds": input_drain_timeout,
            "input_drain_polls_upper_bound": drain_polls,
            "capacity_refusal_audit_reads_upper_bound": turns + 16 + same_session_requests,
            "capacity_recovery_probe_turns_upper_bound": 6,
            "estimated_http_requests_upper_bound": estimated}


def percentile(values: list[float], quantile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil(quantile * len(ordered)) - 1))
    return round(ordered[index], 3)


def summarize_sse(body: bytes) -> dict[str, Any]:
    text = body.decode("utf-8", errors="replace")
    events: list[str] = []
    for block in text.split("\n\n"):
        event_name = None
        data_lines = []
        for line in block.splitlines():
            if line.startswith("event:"):
                event_name = line[6:].strip()
            elif line.startswith("data:"):
                data_lines.append(line[5:].strip())
        if data_lines:
            events.append(event_name or "message")
    failed = any(name == "run_failed" or name.endswith("_failed") or name in {"error", "run_timed_out"}
                 for name in events)
    return {"event_count": len(events), "failed_event": failed}


def summarize_live_sse(body: bytes) -> dict[str, Any]:
    summary = summarize_sse(body)
    run_id = None
    terminal = False
    for block in body.decode("utf-8", errors="replace").split("\n\n"):
        event_name = ""
        data_lines = []
        for line in block.splitlines():
            if line.startswith("event:"):
                event_name = line[6:].strip()
            elif line.startswith("data:"):
                data_lines.append(line[5:].strip())
        if data_lines:
            try:
                payload = json.loads("\n".join(data_lines))
            except json.JSONDecodeError:
                continue
            if run_id is None and isinstance(payload, dict) and isinstance(payload.get("run_id"), str):
                run_id = payload["run_id"]
            terminal = terminal or event_name in {"run_completed", "run_failed", "run_cancelled"}
    return {**summary, "run_id": run_id, "terminal_event": terminal}


class RequestBook:
    def __init__(self, name: str):
        self.name = name
        self._lock = threading.Lock()
        self.rows: list[dict[str, Any]] = []
        self.invariants: list[str] = []
        self.expected_conflicts = 0

    def add(self, operation: str, status: int | None, elapsed_ms: float, error: str | None = None):
        with self._lock:
            self.rows.append({"operation": operation, "status": status,
                              "elapsed_ms": elapsed_ms, "error_category": error})

    def fail(self, category: str):
        with self._lock:
            self.invariants.append(category)

    def conflict(self):
        with self._lock:
            self.expected_conflicts += 1

    @staticmethod
    def _group_stats(group, elapsed_seconds):
        values = [row["elapsed_ms"] for row in group]
        return {"requests": len(group), "p50_ms": percentile(values, .50),
                "p95_ms": percentile(values, .95), "p99_ms": percentile(values, .99),
                "throughput_rps": round(len(group) / elapsed_seconds, 3) if elapsed_seconds > 0 else 0}

    def summary(self, elapsed_seconds: float, *, extras: dict[str, Any] | None = None) -> dict[str, Any]:
        rows = list(self.rows)
        status_counts = Counter(str(row["status"]) if row["status"] is not None else "transport_error"
                                for row in rows)
        errors = Counter(row["error_category"] for row in rows if row["error_category"])
        by_operation: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in rows:
            by_operation[row["operation"]].append(row)
        result = {"name": self.name, "elapsed_seconds": round(elapsed_seconds, 4),
                  "request_count": len(rows), "throughput_rps": round(len(rows) / elapsed_seconds, 3)
                  if elapsed_seconds > 0 else 0,
                  "latency": self._group_stats(rows, elapsed_seconds),
                  "by_operation": {name: self._group_stats(group, elapsed_seconds)
                                   for name, group in sorted(by_operation.items())},
                  "status_distribution": dict(sorted(status_counts.items())),
                  "expected_conflicts": self.expected_conflicts,
                  "error_categories": dict(sorted(errors.items())),
                  "invariant_failures": dict(Counter(self.invariants))}
        if extras:
            result.update(extras)
        return result


class LabHttpClient:
    def __init__(self, base: urllib.parse.SplitResult, timeout: float):
        self.base = base
        self.timeout = timeout
        self.origin = urllib.parse.urlunsplit((base.scheme, base.netloc, "", "", ""))
        self.cookie = ""
        self.csrf = ""
        self._opener = urllib.request.build_opener(NoRedirect())

    def _url(self, path: str) -> str:
        return urllib.parse.urlunsplit((self.base.scheme, self.base.netloc, path, "", ""))

    @staticmethod
    def _read_bounded(response) -> bytes:
        body = response.read(_MAX_RESPONSE_BYTES + 1)
        if len(body) > _MAX_RESPONSE_BYTES:
            raise LabInputError("response exceeded safety size limit")
        return body

    def request(self, method: str, path: str, book: RequestBook, operation: str,
                body: dict[str, Any] | None = None, *, expected: set[int] | None = None,
                auth: bool = True, stream: bool = False, capture_cookie: bool = False,
                timeout: float | None = None, live_stream: bool = False) -> tuple[int | None, Any]:
        headers = {"Accept": "text/event-stream" if stream else "application/json",
                   "Origin": self.origin}
        data = None
        if body is not None:
            data = json.dumps(body, separators=(",", ":")).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if auth:
            headers.update({"Cookie": self.cookie, "X-CSRF-Token": self.csrf})
        started = time.perf_counter()
        status = None
        category = None
        try:
            req = urllib.request.Request(self._url(path), data=data, headers=headers, method=method)
            with self._opener.open(req, timeout=timeout or self.timeout) as response:
                status = response.status
                if capture_cookie:
                    self._capture_cookie(response.headers.get_all("Set-Cookie", []))
                raw = self._read_bounded(response)
            if ((expected is not None and status not in expected)
                    or status < 200 or status >= 300):
                category = f"http_{status}"
            elif stream:
                return self._record_stream(book, operation, status, started, raw, category,
                                           live=live_stream)
            elif raw:
                try:
                    return self._record_json(book, operation, status, started, json.loads(raw), category)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    category = "invalid_json"
                    return self._record_json(book, operation, status, started, None, category)
            return self._record_json(book, operation, status, started, {}, category)
        except urllib.error.HTTPError as exc:
            status = exc.code
            category = None if expected is not None and status in expected else f"http_{status}"
            payload = None
            try:
                raw = exc.read(_MAX_RESPONSE_BYTES + 1)
                if len(raw) > _MAX_RESPONSE_BYTES:
                    category = "response_too_large"
                elif raw:
                    try:
                        payload = json.loads(raw)
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        if category is None:
                            category = "invalid_json"
            except OSError:
                if category is None:
                    category = "error_body_unavailable"
            exc.close()
            return self._record_json(book, operation, status, started, payload, category)
        except (OSError, TimeoutError, http.client.HTTPException, urllib.error.URLError) as exc:
            category = f"transport_{type(exc).__name__}"
        except LabInputError:
            category = "response_too_large"
        return self._record_json(book, operation, status, started, None, category)

    def _capture_cookie(self, set_cookie_headers: list[str]) -> None:
        for header in set_cookie_headers:
            parsed = SimpleCookie()
            parsed.load(header)
            if parsed:
                name = next(iter(parsed))
                self.cookie = f"{name}={parsed[name].value}"
                return

    @staticmethod
    def _record_json(book, operation, status, started, payload, category):
        book.add(operation, status, (time.perf_counter() - started) * 1000, category)
        return status, payload

    @staticmethod
    def _record_stream(book, operation, status, started, body, category, *, live=False):
        payload = summarize_live_sse(body) if live else summarize_sse(body)
        if payload["failed_event"]:
            category = "failed_sse_event"
        elif payload["event_count"] == 0:
            category = "empty_sse_stream"
        book.add(operation, status, (time.perf_counter() - started) * 1000, category)
        return status, payload

    def authenticate(self, login: dict[str, str], book: RequestBook) -> bool:
        status, _ = self.request("POST", "/auth/login", book, "login", login,
                                 expected={200}, auth=False, capture_cookie=True)
        if status != 200 or not self.cookie:
            book.fail("login_failed")
            return False
        status, result = self.request("GET", "/auth/me", book, "auth_me")
        csrf = result.get("csrf_token") if isinstance(result, dict) else None
        if status != 200 or not isinstance(csrf, str) or not csrf:
            book.fail("csrf_unavailable")
            return False
        self.csrf = csrf
        return True

    def health(self, book: RequestBook, operation: str) -> bool:
        status, result = self.request("GET", "/health", book, operation, auth=False)
        healthy = status == 200 and isinstance(result, dict) and result.get("status") == "ok"
        if not healthy:
            book.fail("health_not_ok")
        return healthy


class StressRunner:
    def __init__(self, client: LabHttpClient, turns_per_level: int, same_session_requests: int,
                 sse_requests: int, input_drain_timeout: float):
        self.client = client
        self.turns_per_level = turns_per_level
        self.same_session_requests = same_session_requests
        self.sse_requests = sse_requests
        self.input_drain_timeout = input_drain_timeout
        self.prefix = "stress_" + secrets.token_hex(8)
        self.run_ids: list[str] = []
        self.session_counter = 0

    def _id(self, label: str) -> str:
        return f"{self.prefix}_{label}_{secrets.token_hex(5)}"

    def create_session(self, book: RequestBook, operation="session_create") -> str | None:
        self.session_counter += 1
        title = f"isolated stress {self.prefix[-8:]} {self.session_counter}"
        status, result = self.client.request("POST", "/sessions", book, operation,
                                             {"title": title, "metadata": {"source": "synthetic_http_stress"}})
        session = result.get("session") if isinstance(result, dict) else None
        session_id = session.get("session_id") if isinstance(session, dict) else None
        if status != 200 or not isinstance(session_id, str) or not session_id:
            book.fail("session_create_failed")
            return None
        return session_id

    def run_turn(self, session_id: str, client_request_id: str, book: RequestBook,
                 *, operation="turn_post", expected: set[int] | None = None) -> tuple[int | None, str | None, dict[str, Any] | None]:
        status, result = self.client.request("POST", "/agent/turn", book, operation, {
            "session_id": session_id,
            "client_request_id": client_request_id,
            "user_input": "请对这条隔离压测消息简短确认，不要调用工具。",
        }, expected=expected or {200})
        run_id = result.get("run_id") if isinstance(result, dict) else None
        if status in {200, 202} and isinstance(run_id, str):
            return status, run_id, result
        return status, None, result if isinstance(result, dict) else None

    def verify_run(self, session_id: str, run_id: str, book: RequestBook,
                   *, history_operation="history_get") -> bool:
        ok = True
        status, run = self.client.request("GET", f"/agent/runs/{urllib.parse.quote(run_id, safe='')}",
                                          book, "run_get")
        run_status = run.get("status") if isinstance(run, dict) else None
        if status != 200 or run_status != "completed":
            book.fail("run_not_completed_or_missing")
            ok = False
        status, events = self.client.request(
            "GET", f"/agent/runs/{urllib.parse.quote(run_id, safe='')}/events", book, "events_get")
        event_list = events.get("events") if isinstance(events, dict) else None
        if status != 200 or not isinstance(event_list, list) or not event_list:
            book.fail("durable_events_missing")
            ok = False
        elif any(event.get("type") == "run_failed" or str(event.get("type", "")).endswith("_failed")
                 for event in event_list if isinstance(event, dict)):
            book.fail("failed_event_persisted")
            ok = False
        status, history = self.client.request(
            "GET", f"/sessions/{urllib.parse.quote(session_id, safe='')}?message_limit=20",
            book, history_operation)
        messages = history.get("messages") if isinstance(history, dict) else None
        if status != 200 or not isinstance(messages, list) or not messages:
            book.fail("session_history_missing")
            ok = False
        return ok

    @staticmethod
    def _is_capacity_refusal(status: int | None, payload: Any) -> bool:
        return (status == 503 and isinstance(payload, dict)
                and payload.get("detail") == "agent_execution_capacity_full"
                and payload.get("accepted") is False
                and payload.get("retry_safe") is True)

    def _verify_no_run_for_request(self, session_id: str, request_id: str,
                                   book: RequestBook, *, operation: str) -> bool:
        path = (f"/sessions/{urllib.parse.quote(session_id, safe='')}/runs?client_request_id="
                + urllib.parse.quote(request_id, safe=""))
        status, listing = self.client.request("GET", path, book, operation)
        runs = listing.get("runs") if isinstance(listing, dict) else None
        if status != 200 or not isinstance(runs, list) or runs:
            book.fail("refused_request_persisted_run")
            return False
        return True

    def _capacity_recovery_probe(self, session_id: str, book: RequestBook,
                                 *, label: str) -> bool:
        """Use a fresh correlation ID to confirm capacity recovers after refusal."""
        status, run_id, _ = self.run_turn(
            session_id, self._id(f"capacity_recovery_{label}"), book,
            operation="capacity_recovery_post", expected={200, 202},
        )
        if status not in {200, 202} or run_id is None:
            book.fail("capacity_recovery_post_failed")
            return False
        self.run_ids.append(run_id)
        return self.verify_run(session_id, run_id, book, history_operation="capacity_recovery_history")

    def run_levels(self, common: RequestBook) -> list[dict[str, Any]]:
        results = []
        for workers in (1, 4, 8, 16):
            book = RequestBook(f"agent_turns_concurrency_{workers}")
            self.client.health(common, f"health_before_c{workers}")
            sessions = [self.create_session(common) for _ in range(self.turns_per_level)]
            if any(session is None for session in sessions):
                book.fail("session_setup_failed")
                results.append(book.summary(0, extras={"configured_concurrency": workers,
                    "planned_turns": self.turns_per_level,
                    "requested_turns": self.turns_per_level, "accepted_turns": 0,
                    "completed_turns": 0, "refused_turns": 0,
                    "recovery_probe_passed": False}))
                self.client.health(common, f"health_after_c{workers}")
                continue
            accepted = 0
            completed = 0
            refused = 0
            start = time.perf_counter()
            turn_latencies = []
            # The simultaneous history reader participates in the barrier too.
            with concurrent.futures.ThreadPoolExecutor(max_workers=workers + 1) as pool:
                for offset in range(0, len(sessions), workers):
                    batch = sessions[offset:offset + workers]
                    barrier = threading.Barrier(len(batch) + (1 if self.run_ids else 0))
                    futures = []
                    # One real history read is released with each write batch;
                    # it targets a synthetic session containing prior history.
                    history_future = None
                    if self.run_ids:
                        history_session = self._history_seed_session
                        history_future = pool.submit(self._concurrent_history, history_session, book, barrier)
                    for index, session_id in enumerate(batch):
                        request_id = self._id(f"c{workers}_turn{offset + index}")
                        futures.append(pool.submit(self._turn_and_verify, session_id, request_id, book, barrier,
                                                   turn_latencies))
                    for future in futures:
                        accepted_one, completed_one, refused_one = future.result()
                        accepted += accepted_one
                        completed += completed_one
                        refused += refused_one
                    if history_future is not None and not history_future.result():
                        book.fail("mixed_history_read_failed")
            recovery_ok = True
            if refused:
                recovery_ok = self._capacity_recovery_probe(
                    sessions[0], book, label=f"level_{workers}",
                )
            elapsed = time.perf_counter() - start
            self.client.health(common, f"health_after_c{workers}")
            results.append(book.summary(elapsed, extras={
                "configured_concurrency": workers, "planned_turns": len(sessions),
                "requested_turns": len(sessions), "accepted_turns": accepted,
                "completed_turns": completed, "refused_turns": refused,
                "recovery_probe_passed": recovery_ok,
                "request_throughput_scope": (
                    "all HTTP requests, including verification and capacity-recovery probes; "
                    "refusals are excluded from verified turn throughput"
                ),
                "verified_turn_throughput_rps": round(completed / elapsed, 3) if elapsed > 0 else 0,
                "turn_post_latency_p50_ms": percentile(turn_latencies, .50),
                "turn_post_latency_p95_ms": percentile(turn_latencies, .95),
                "turn_post_latency_p99_ms": percentile(turn_latencies, .99),
            }))
        return results

    def _concurrent_history(self, session_id, book, barrier):
        barrier.wait(timeout=self.client.timeout)
        status, result = self.client.request(
            "GET", f"/sessions/{urllib.parse.quote(session_id, safe='')}?message_limit=20",
            book, "mixed_history_get")
        good = status == 200 and isinstance(result, dict) and isinstance(result.get("messages"), list)
        if not good:
            book.fail("mixed_history_read_failed")
        return good

    def _turn_and_verify(self, session_id, request_id, book, barrier, latencies):
        barrier.wait(timeout=self.client.timeout)
        started = time.perf_counter()
        status, run_id, payload = self.run_turn(
            session_id, request_id, book, expected={200, 202, 503},
        )
        latencies.append((time.perf_counter() - started) * 1000)
        if self._is_capacity_refusal(status, payload):
            self._verify_no_run_for_request(
                session_id, request_id, book, operation="capacity_refusal_runs_get",
            )
            return 0, 0, 1
        if status not in {200, 202} or run_id is None:
            book.fail("turn_post_failed")
            return 0, 0, 0
        self.run_ids.append(run_id)
        completed = self.verify_run(session_id, run_id, book)
        return 1, int(completed), 0

    def run_request_correlation(self, common: RequestBook) -> dict[str, Any]:
        # client_request_id is correlation-only in the published API contract.
        # Actual idempotency is checked by the durable input queue below.
        book = RequestBook("request_correlation")
        self.client.health(common, "health_before_correlation")
        start = time.perf_counter()
        session_id = self.create_session(book)
        correlated_count = 0
        if session_id:
            request_id = self._id("correlation")
            status1, run1, _ = self.run_turn(session_id, request_id, book, operation="turn_first")
            status2, run2, _ = self.run_turn(session_id, request_id, book, operation="turn_replay")
            if status1 != 200 or status2 != 200 or not run1 or not run2 or run1 == run2:
                book.fail("correlation_two_explicit_posts_not_two_runs")
            listing_status, listing = self.client.request(
                "GET", f"/sessions/{urllib.parse.quote(session_id, safe='')}/runs?client_request_id="
                       + urllib.parse.quote(request_id, safe=""), book, "runs_by_request_id")
            runs = listing.get("runs") if isinstance(listing, dict) else None
            if (listing_status != 200 or not isinstance(runs, list) or len(runs) != 2
                    or {row.get("run_id") for row in runs} != {run1, run2}
                    or any(row.get("client_request_id") != request_id for row in runs)):
                book.fail("request_correlation_query_mismatch")
            else:
                correlated_count = len(runs)
            for run_id in (run1, run2):
                if run_id:
                    self.verify_run(session_id, run_id, book)
                    self.run_ids.append(run_id)
        self.client.health(common, "health_after_correlation")
        return book.summary(time.perf_counter() - start, extras={
            "correlated_run_count": correlated_count,
            "contract": "client_request_id correlates; repeated POST is not idempotent",
        })

    def run_same_session(self, common: RequestBook) -> dict[str, Any]:
        book = RequestBook("same_session_contention")
        self.client.health(common, "health_before_same_session")
        start = time.perf_counter()
        session_id = self.create_session(book)
        accepted: list[str] = []
        refused = 0
        completed = 0
        if session_id:
            workers = self.same_session_requests
            barrier = threading.Barrier(workers)
            def contender(index):
                barrier.wait(timeout=self.client.timeout)
                request_id = self._id(f"same_session_{index}")
                status, run_id, payload = self.run_turn(
                    session_id, request_id, book,
                    operation="same_session_turn", expected={200, 202, 409, 503})
                if status == 409:
                    book.conflict()
                    self._verify_no_run_for_request(
                        session_id, request_id, book, operation="conflict_refusal_runs_get",
                    )
                    return status, None, request_id, payload
                if self._is_capacity_refusal(status, payload):
                    self._verify_no_run_for_request(
                        session_id, request_id, book, operation="capacity_refusal_runs_get",
                    )
                    return status, None, request_id, payload
                return status, run_id, request_id, payload
            with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
                futures = [pool.submit(contender, index) for index in range(workers)]
                for future in futures:
                    status, run_id, _request_id, payload = future.result()
                    if status in {200, 202} and run_id:
                        accepted.append(run_id)
                    elif status == 409:
                        refused += 1
                    elif status == 503:
                        if self._is_capacity_refusal(status, payload):
                            refused += 1
                        else:
                            book.fail("same_session_request_failed")
                    elif status is not None:
                        book.fail("same_session_request_failed")
                    else:
                        book.fail("same_session_request_failed")
            intervals = []
            for run_id in accepted:
                self.run_ids.append(run_id)
                status, run = self.client.request(
                    "GET", f"/agent/runs/{urllib.parse.quote(run_id, safe='')}", book, "same_session_run_get")
                if status != 200 or not isinstance(run, dict):
                    book.fail("same_session_run_missing")
                    continue
                if run.get("status") != "completed":
                    book.fail("same_session_run_not_completed")
                started = _parse_time(run.get("started_at"))
                ended = _parse_time(run.get("completed_at"))
                if started is None:
                    book.fail("same_session_run_start_missing")
                else:
                    intervals.append((started, ended))
                if run.get("status") == "completed":
                    completed += int(self.verify_run(session_id, run_id, book))
            for i, (left_start, left_end) in enumerate(intervals):
                for right_start, right_end in intervals[i + 1:]:
                    if _intervals_overlap(left_start, left_end, right_start, right_end):
                        book.fail("overlapping_root_run_intervals")
            listing_status, listing = self.client.request(
                "GET", f"/sessions/{urllib.parse.quote(session_id, safe='')}/runs", book, "same_session_runs_get")
            persisted = listing.get("runs") if isinstance(listing, dict) else None
            if listing_status != 200 or not isinstance(persisted, list) or len(persisted) != len(accepted):
                book.fail("same_session_root_count_mismatch")
        self.client.health(common, "health_after_same_session")
        if not accepted:
            book.fail("same_session_no_request_accepted")
        recovery_ok = True
        if session_id and refused:
            recovery_ok = self._capacity_recovery_probe(
                session_id, book, label="same_session",
            )
        return book.summary(time.perf_counter() - start, extras={
            "configured_concurrency": self.same_session_requests,
            "requested_requests": self.same_session_requests,
            "accepted_requests": len(accepted), "completed_requests": completed,
            "refused_requests": refused, "expected_409_conflicts": book.expected_conflicts,
            "recovery_probe_passed": recovery_ok,
            "request_throughput_scope": (
                "all HTTP requests, including no-run audits and the distinct recovery probe"
            ),
            "accepted_runs_nonoverlapping": "overlapping_root_run_intervals" not in book.invariants,
        })

    def run_sse(self, common: RequestBook) -> dict[str, Any]:
        book = RequestBook("sse_reconnect")
        self.client.health(common, "health_before_sse")
        if not self.run_ids:
            book.fail("no_completed_runs_for_sse")
            self.client.health(common, "health_after_sse")
            return book.summary(0, extras={"configured_concurrency": min(8, self.sse_requests),
                                           "planned_streams": self.sse_requests})
        selected = [self.run_ids[index % len(self.run_ids)] for index in range(self.sse_requests)]
        start = time.perf_counter()
        def fetch(run_id):
            status, summary = self.client.request(
                "GET", f"/agent/runs/{urllib.parse.quote(run_id, safe='')}/stream?after_sequence=0",
                book, "sse_get", stream=True)
            good = status == 200 and isinstance(summary, dict) and summary.get("event_count", 0) > 0
            if not good:
                book.fail("sse_stream_invalid")
            return good
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(8, self.sse_requests)) as pool:
            good = sum(bool(value) for value in pool.map(fetch, selected))
        elapsed = time.perf_counter() - start
        self.client.health(common, "health_after_sse")
        return book.summary(elapsed, extras={"configured_concurrency": min(8, self.sse_requests),
                                             "planned_streams": self.sse_requests,
                                             "valid_streams": good})

    def run_live_sse(self, common: RequestBook) -> dict[str, Any]:
        """Run fresh streamed turns concurrently and verify their durable results."""
        book = RequestBook("live_sse_turns")
        self.client.health(common, "health_before_live_sse")
        sessions = [self.create_session(book, "live_sse_session_create") for _ in range(16)]
        if any(session is None for session in sessions):
            book.fail("live_sse_session_setup_failed")
            self.client.health(common, "health_after_live_sse")
            return book.summary(0, extras={"configured_concurrency": 8, "planned_streams": 16,
                                           "requested_streams": 16, "accepted_streams": 0,
                                           "completed_streams": 0, "refused_streams": 0,
                                           "recovery_probe_passed": False})
        started = time.perf_counter()
        accepted = 0
        completed = 0
        refused = 0

        def stream_turn(index, session_id, barrier):
            barrier.wait(timeout=self.client.timeout)
            request_id = self._id(f"live_sse_{index}")
            status, result = self.client.request("POST", "/agent/turn/stream", book, "live_sse_post", {
                "session_id": session_id, "client_request_id": request_id,
                "user_input": "请对隔离压测消息简短确认，不要调用工具。",
            }, expected={200, 503}, stream=True, live_stream=True)
            if self._is_capacity_refusal(status, result):
                self._verify_no_run_for_request(
                    session_id, request_id, book, operation="capacity_refusal_runs_get",
                )
                return "refused"
            run_id = result.get("run_id") if isinstance(result, dict) else None
            if status != 200 or not isinstance(run_id, str):
                book.fail("live_sse_stream_not_terminal_or_failed")
                return "failed"
            self.run_ids.append(run_id)
            if not result.get("terminal_event") or result.get("failed_event"):
                book.fail("live_sse_stream_not_terminal_or_failed")
                self.verify_run(session_id, run_id, book, history_operation="live_sse_history_get")
                return "accepted"
            if self.verify_run(session_id, run_id, book, history_operation="live_sse_history_get"):
                return "completed"
            return "accepted"

        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            for offset in (0, 8):
                barrier = threading.Barrier(8)
                futures = [pool.submit(stream_turn, index, sessions[index], barrier)
                           for index in range(offset, offset + 8)]
                for future in futures:
                    outcome = future.result()
                    if outcome == "completed":
                        accepted += 1
                        completed += 1
                    elif outcome == "accepted":
                        accepted += 1
                    elif outcome == "refused":
                        refused += 1
        recovery_ok = True
        if refused:
            recovery_ok = self._capacity_recovery_probe(
                sessions[0], book, label="live_sse",
            )
        elapsed = time.perf_counter() - started
        self.client.health(common, "health_after_live_sse")
        return book.summary(elapsed, extras={"configured_concurrency": 8, "planned_streams": 16,
                                             "requested_streams": 16,
                                             "accepted_streams": accepted,
                                             "completed_streams": completed,
                                             "refused_streams": refused,
                                             "recovery_probe_passed": recovery_ok,
                                             "request_throughput_scope": (
                                                 "all HTTP requests, including verification and "
                                                 "capacity-recovery probes; refusals are excluded "
                                                 "from completed streams"
                                             )})

    def run_input_burst(self, common: RequestBook) -> dict[str, Any]:
        """Burst exact-idempotent input submissions, then verify durable drain."""
        book = RequestBook("persistent_input_burst")
        self.client.health(common, "health_before_input_burst")
        start = time.perf_counter()
        manager_status, manager = self.client.request(
            "POST", "/managers", book, "manager_create",
            {"title": f"stress inputs {self.prefix[-8:]}", "make_default": True}, expected={201},
        )
        session = manager.get("session") if isinstance(manager, dict) else None
        session_id = session.get("session_id") if isinstance(session, dict) else None
        if manager_status != 201 or not isinstance(session_id, str) or not session_id:
            book.fail("manager_create_failed")
            self.client.health(common, "health_after_input_burst")
            return book.summary(time.perf_counter() - start, extras={"planned_attempts": 70,
                "accepted_attempts": 0, "unique_inputs": 64, "drained": False})

        attempts = self._input_attempts()
        accepted_receipts: list[dict[str, Any] | None] = [None] * len(attempts)
        accept_started = time.perf_counter()
        for offset in range(0, len(attempts), 16):
            batch = attempts[offset:offset + 16]
            barrier = threading.Barrier(len(batch))
            def submit_one(pair, *, barrier=barrier):
                index, item = pair
                barrier.wait(timeout=self.client.timeout)
                status, result = self.client.request(
                    "POST", f"/sessions/{urllib.parse.quote(session_id, safe='')}/inputs",
                    book, "input_submit", item, expected={202},
                )
                receipt = result.get("input") if isinstance(result, dict) else None
                if status != 202 or not isinstance(receipt, dict):
                    book.fail("input_submission_failed")
                else:
                    accepted_receipts[index] = receipt
                return status
            with concurrent.futures.ThreadPoolExecutor(max_workers=16) as pool:
                futures = [pool.submit(submit_one, (offset + index, item))
                           for index, item in enumerate(batch)]
                for future in futures:
                    try:
                        future.result()
                    except Exception as exc:  # noqa: BLE001 - preserve body-free failure category.
                        book.fail(f"input_worker_{type(exc).__name__}")
        accept_seconds = time.perf_counter() - accept_started

        expected_by_client_id: dict[str, str] = {}
        expected_message_by_client_id: dict[str, str] = {}
        attempts_by_client_id: Counter[str] = Counter()
        for item in attempts:
            attempts_by_client_id[item["client_message_id"]] += 1
        for item, receipt in zip(attempts, accepted_receipts):
            if receipt is None:
                continue
            client_message_id = item["client_message_id"]
            input_id = receipt.get("input_id")
            if not isinstance(input_id, str) or not input_id:
                book.fail("input_receipt_missing_id")
                continue
            prior = expected_by_client_id.setdefault(client_message_id, input_id)
            if prior != input_id:
                book.fail("duplicate_receipt_not_idempotent")
            message_id = receipt.get("message_id")
            if not isinstance(message_id, str) or not message_id:
                book.fail("input_receipt_missing_message_id")
            else:
                prior_message = expected_message_by_client_id.setdefault(client_message_id, message_id)
                if prior_message != message_id:
                    book.fail("duplicate_message_receipt_not_idempotent")
        if len(expected_by_client_id) != 64:
            book.fail("unique_receipt_count_mismatch")
        if sum(attempts_by_client_id.values()) != 70 or len(attempts_by_client_id) != 64:
            book.fail("synthetic_duplicate_plan_invalid")

        drain_started = time.monotonic()
        deadline = drain_started + self.input_drain_timeout
        latest_snapshot = None
        polls = 0
        while time.monotonic() < deadline:
            polls += 1
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            status, snapshot = self.client.request(
                "GET", f"/sessions/{urllib.parse.quote(session_id, safe='')}/inputs",
                book, "input_queue_poll", timeout=min(self.client.timeout, remaining),
            )
            if status != 200 or not isinstance(snapshot, dict):
                book.fail("input_queue_snapshot_failed")
                break
            latest_snapshot = snapshot
            if snapshot.get("pending_count") == 0 and snapshot.get("active_run_id") is None:
                break
            time.sleep(.5)
        drain_seconds = time.monotonic() - drain_started
        drained = (isinstance(latest_snapshot, dict) and latest_snapshot.get("pending_count") == 0
                   and latest_snapshot.get("active_run_id") is None)
        if not drained:
            book.fail("input_queue_drain_timeout_or_still_active")

        input_rows = latest_snapshot.get("inputs") if isinstance(latest_snapshot, dict) else None
        if not isinstance(input_rows, list):
            input_rows = []
            book.fail("input_queue_rows_missing")
        final_by_client_id = {}
        for row in input_rows:
            if isinstance(row, dict) and isinstance(row.get("client_message_id"), str):
                final_by_client_id[row["client_message_id"]] = row
        if set(final_by_client_id) != set(expected_by_client_id):
            book.fail("input_queue_unique_membership_mismatch")
        if any(final_by_client_id.get(cid, {}).get("input_id") != iid
               for cid, iid in expected_by_client_id.items()):
            book.fail("input_queue_receipt_id_mismatch")
        if any(final_by_client_id.get(cid, {}).get("message_id") != mid
               for cid, mid in expected_message_by_client_id.items()):
            book.fail("input_queue_message_id_mismatch")
        statuses = Counter(row.get("status", "missing") for row in input_rows if isinstance(row, dict))
        if len(final_by_client_id) != 64 or statuses.get("completed", 0) != 64:
            book.fail("input_not_all_completed")
        if any(status in {"failed", "cancelled", "running", "queued"}
               for status in statuses):
            book.fail("input_failure_or_nonterminal_status")

        messages_by_id = {}
        history_status = 200
        before_message_id = None
        history_pages = 0
        for _ in range(3):
            history_pages += 1
            query = "?message_limit=100"
            if before_message_id:
                query += "&before_message_id=" + urllib.parse.quote(before_message_id, safe="")
            history_status, history = self.client.request(
                "GET", f"/sessions/{urllib.parse.quote(session_id, safe='')}{query}",
                book, "input_history_verify",
            )
            messages = history.get("messages") if isinstance(history, dict) else None
            if history_status != 200 or not isinstance(messages, list):
                break
            messages_by_id.update({item.get("message_id"): item.get("content") for item in messages
                                   if isinstance(item, dict)})
            page = history.get("message_page")
            if not isinstance(page, dict) or page.get("has_more") is not True:
                break
            before_message_id = page.get("first_message_id")
            if not isinstance(before_message_id, str) or not before_message_id:
                book.fail("input_history_cursor_missing")
                break
        else:
            book.fail("input_history_page_limit_exceeded")
        message_ids = set(messages_by_id)
        expected_message_ids = {row.get("message_id") for row in input_rows if isinstance(row, dict)}
        if history_status != 200 or not expected_message_ids <= message_ids:
            book.fail("input_messages_missing_from_history")
        for item, receipt in zip(attempts, accepted_receipts):
            if receipt is None:
                continue
            if messages_by_id.get(receipt.get("message_id")) != item["user_input"]:
                book.fail("input_message_content_mismatch")

        self.client.health(common, "health_after_input_burst")
        extras = {"configured_concurrency": 16, "planned_attempts": len(attempts),
                  "accepted_attempts": sum(receipt is not None for receipt in accepted_receipts),
                  "unique_inputs": 64, "unique_receipts": len(expected_by_client_id),
                  "duplicate_attempts": len(attempts) - len(attempts_by_client_id),
                  "receipt_ids_stable": "duplicate_receipt_not_idempotent" not in book.invariants,
                  "accept_elapsed_seconds": round(accept_seconds, 4),
                  "accept_latency_p95_ms": percentile([row["elapsed_ms"] for row in book.rows
                                                       if row["operation"] == "input_submit"], .95),
                  "accept_latency_p99_ms": percentile([row["elapsed_ms"] for row in book.rows
                                                       if row["operation"] == "input_submit"], .99),
                  "drain_elapsed_seconds": round(drain_seconds, 4), "drain_polls": polls,
                  "drained": drained, "history_pages": history_pages,
                  "final_status_distribution": dict(sorted(statuses.items()))}
        return book.summary(time.perf_counter() - start, extras=extras)

    def _input_attempts(self) -> list[dict[str, Any]]:
        unique_count = 64
        base = []
        by_id = {}
        for index in range(unique_count):
            client_id = self._id(f"input_{index}")
            item = {"client_message_id": client_id,
                    "user_input": f"Synthetic isolated queue burst item {index} {self.prefix}",
                    "attachment_ids": [], "safety_review_mode": "skip"}
            by_id[client_id] = item
            base.append(item)
        attempts = [dict(item) for item in base]
        attempts.extend(dict(by_id[base[index]["client_message_id"]]) for index in range(6))
        import random
        random.Random(42).shuffle(attempts)
        return attempts

    def run(self) -> dict[str, Any]:
        common = RequestBook("setup_and_health")
        started_at = datetime.now(UTC).isoformat()
        self.client.health(common, "health_initial")
        if not self.client.authenticate(self.login, common):
            return {"started_at": started_at, "outcome": "failed", "failure": "authentication_failed",
                    "setup": common.summary(0)}
        self.client.health(common, "health_authenticated")
        self._seed_history(common)
        levels = self.run_levels(common)
        correlation = self.run_request_correlation(common)
        same_session = self.run_same_session(common)
        sse = self.run_sse(common)
        live_sse = self.run_live_sse(common)
        input_burst = self.run_input_burst(common)
        failed = bool(common.invariants or any(row["error_category"] for row in common.rows))
        all_scenarios = [*levels, correlation, same_session, sse, live_sse, input_burst]
        for scenario in all_scenarios:
            failed = failed or bool(scenario.get("error_categories")) or bool(scenario.get("invariant_failures"))
        return {"started_at": started_at, "finished_at": datetime.now(UTC).isoformat(),
                "outcome": "failed" if failed else "passed", "base_url": self.client.origin,
                "plan": self.plan, "setup": common.summary(0), "scenarios": {
                    "agent_turns": levels, "request_correlation": correlation,
                    "same_session": same_session, "sse": sse,
                    "live_sse_turns": live_sse,
                    "persistent_input_burst": input_burst}}

    def _seed_history(self, common):
        self._history_seed_session = self.create_session(common, "seed_session_create")
        if self._history_seed_session is None:
            common.fail("history_seed_session_failed")
            return
        status, run_id, _ = self.run_turn(self._history_seed_session, self._id("history_seed"), common,
                                          operation="seed_turn_post")
        if status == 200 and run_id:
            self.run_ids.append(run_id)
            self.verify_run(self._history_seed_session, run_id, common, history_operation="seed_history_get")
        else:
            common.fail("history_seed_turn_failed")

    def configure(self, login, plan):
        self.login = login
        self.plan = plan


def _parse_time(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
        return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)
    except ValueError:
        return None


def _intervals_overlap(first_start, first_end, second_start, second_end):
    first_end = first_end or datetime.max.replace(tzinfo=UTC)
    second_end = second_end or datetime.max.replace(tzinfo=UTC)
    return first_start < second_end and second_start < first_end


def _read_login(path: Path) -> dict[str, str]:
    try:
        payload = json.loads(path.expanduser().resolve(strict=True).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise LabInputError("could not read login JSON") from exc
    if not isinstance(payload, dict) or set(payload) != {"username", "password"}:
        raise LabInputError("login JSON must contain only username and password")
    if (not isinstance(payload["username"], str) or not payload["username"]
            or not isinstance(payload["password"], str) or not payload["password"]):
        raise LabInputError("login JSON credentials must be non-empty strings")
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True, help="isolated API on loopback, e.g. http://127.0.0.1:8768")
    parser.add_argument("--login-file", type=Path, required=True, help="private JSON with username/password")
    parser.add_argument("--output", type=Path, required=True, help="credential-free JSON report")
    parser.add_argument("--turns-per-level", type=int, default=24,
                        help="complete turns at each concurrency level 1,4,8,16 (default: 24)")
    parser.add_argument("--same-session-requests", type=int, default=8)
    parser.add_argument("--sse-requests", type=int, default=16)
    parser.add_argument("--input-drain-timeout", type=float, default=120,
                        help="maximum queue drain poll time in seconds, at most 120")
    parser.add_argument("--timeout", type=float, default=60)
    args = parser.parse_args(argv)
    try:
        base = parse_base_url(args.base_url)
        if not math.isfinite(args.timeout) or not 1 <= args.timeout <= 180:
            raise LabInputError("timeout must be between 1 and 180 seconds")
        plan = bounded_plan(args.turns_per_level, args.same_session_requests, args.sse_requests,
                            args.input_drain_timeout)
        login_path = args.login_file.expanduser().resolve(strict=True)
        output_path = args.output.expanduser().resolve()
        if login_path == output_path:
            raise LabInputError("output path must be different from the login file")
        login = _read_login(login_path)
        runner = StressRunner(LabHttpClient(base, args.timeout), args.turns_per_level,
                              args.same_session_requests, args.sse_requests, args.input_drain_timeout)
        runner.configure(login, plan)
        report = runner.run()
        output = output_path
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps({"outcome": report["outcome"], "report": str(output), "plan": plan}, ensure_ascii=False))
        return 0 if report["outcome"] == "passed" else 1
    except LabInputError as exc:
        print(json.dumps({"outcome": "refused", "reason": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 - report only the exception class, never its message.
        safe = {"outcome": "failed", "error_category": type(exc).__name__}
        try:
            output = args.output.expanduser().resolve()
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(json.dumps(safe, indent=2) + "\n", encoding="utf-8")
        except OSError:
            pass
        print(json.dumps(safe), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
