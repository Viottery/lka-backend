"""Atomic publication of an ordinary root turn, without replaying its tools."""
from __future__ import annotations

import json
from contextlib import closing

from app.core.agent_runs import AgentRunCancelled
from app.core.session_admission import SessionAdmissionStore
from app.core.sessions import _rollback_preserving_error
from app.storage.connections import is_mysql, lock_row
from app.storage.mysql import MySQLCommitOutcomeUnknown


class AgentPublicationPaused(RuntimeError):
    """A pause won the publication fence; leave the verified checkpoint intact."""


def publish_root_result(*, manager, sessions, run_id, result, snapshot,
                        user_input, input_role, message_payload, token_budget,
                        background_enqueue=None, message_callback=None):
    """Publish context, message/outbox and terminal event in one transaction.

    An ambiguous COMMIT is checked on a fresh connection under the same session
    fence. Only a fully committed publication is accepted; no write or tool is
    retried. The durable result artifact was already stored by the graph.
    """
    current = manager.get_run(run_id)
    if current is None or current.parent_run_id is not None or manager.durable_store is None:
        raise ValueError("Atomic publication requires a durable root run")
    message_id = f"agent_graph_agent_{run_id}"
    effect_id = f"agent_graph_context_exchange_{run_id}"

    def locked_record(conn):
        conn.execute("BEGIN" if is_mysql(conn) else "BEGIN IMMEDIATE")
        SessionAdmissionStore.lock_session(conn, current.session_id)
        row = lock_row(conn, "agent_runs", "run_id", run_id)
        if row is None:
            raise KeyError(run_id)
        return json.loads(row["record_payload"])

    def committed_publication(conn, record):
        if record.get("status") != "completed":
            return False
        saved = record.get("result_snapshot") or {}
        if any(saved.get(key) != snapshot.get(key) for key in ("session_id", "trace_id", "answer")):
            raise ValueError("Completed run belongs to a different result")
        message = conn.execute(
            "SELECT session_id,content FROM agent_session_messages WHERE message_id=?", (message_id,),
        ).fetchone()
        effect = conn.execute(
            "SELECT session_id,effect_type FROM agent_session_effects WHERE effect_id=?", (effect_id,),
        ).fetchone()
        if (message is None or message["session_id"] != current.session_id
                or message["content"] != result.answer or effect is None
                or effect["session_id"] != current.session_id or effect["effect_type"] != "context_exchange"):
            raise ValueError("Completed run lacks its atomic publication receipts")
        return True

    manager.flush_events()
    try:
        with closing(sessions._conn_factory()) as conn:
            try:
                record = locked_record(conn)
                if not committed_publication(conn, record):
                    metadata = record.get("metadata") or {}
                    if record["status"] == "cancelled" or metadata.get("cancel_requested"):
                        raise AgentRunCancelled("Run cancelled before answer publication")
                    if record["status"] == "paused" or metadata.get("pause_requested"):
                        raise AgentPublicationPaused("Run paused before answer publication")
                    # Completion validates cancellation, pause and descendants
                    # under the same lock. A rejected completion rolls back all
                    # context/message/outbox changes as well.
                    updated = sessions.record_context_exchange_in_transaction(
                        conn, session_id=current.session_id, user_input=user_input,
                        input_role=input_role, agent_answer=result.answer, trace_id=current.trace_id,
                        effect_id=effect_id, token_budget=token_budget,
                        background_enqueue=background_enqueue,
                    )
                    sessions.append_message_in_transaction(
                        conn, session_id=current.session_id, role="agent", content=result.answer,
                        payload={**message_payload, "context_window": {
                            "token_budget": updated.token_budget, "token_estimate": updated.token_estimate,
                            "token_count_method": sessions.context_token_count_method,
                            "recent_message_count": len(updated.recent_messages),
                        }}, message_id=message_id, persisted_message_callback=message_callback,
                    )
                    record = manager.complete_run_in_transaction(
                        conn, run_id, result_snapshot=snapshot, log_path=result.log_path,
                        final_answer_event=next((event.model_dump(mode="json") for event in reversed(result.progress_events)
                                                 if event.type == "final_answer"), None),
                    ).model_dump(mode="json")
                conn.commit()
            except Exception as exc:
                _rollback_preserving_error(conn, exc)
                raise
    except MySQLCommitOutcomeUnknown as uncertain:
        # The original transaction held this anchor until its outcome settled.
        # A locking read avoids accepting a stale pre-commit snapshot. Lock and
        # driver timeouts remain bounded; failures preserve the original error.
        try:
            with closing(sessions._conn_factory()) as conn:
                record = locked_record(conn)
                confirmed = committed_publication(conn, record)
                conn.rollback()
        except Exception as check_error:
            uncertain.add_note("Publication outcome check failed: " + type(check_error).__name__)
            raise uncertain from check_error
        if not confirmed:
            raise
    manager.refresh_after_transaction(record)
    return record
