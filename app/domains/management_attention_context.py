"""Bounded, deterministic attention cards from an already authorized attention batch."""
from __future__ import annotations

import json
from datetime import date, timedelta

from app.domains.management import ManagementConflictError
from app.domains.management_goals import _instant


class ManagementAttentionContext:
    def __init__(self, goals, *, max_chars=4500, max_items=8):
        if type(max_chars) is not int or max_chars < 2:
            raise ValueError("max_chars must be at least 2")
        if type(max_items) is not int or not 1 <= max_items <= 8:
            raise ValueError("max_items must be 1..8")
        self.goals, self.max_chars, self.max_items = goals, max_chars, max_items

    @staticmethod
    def _size(value):
        # Match the compact Unicode encoding used by the management envelope.
        return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")))

    @staticmethod
    def _time(value):
        try:
            return _instant(value) if value else None
        except (ValueError, TypeError):
            return None

    def _task(self, conn, session_id, run_id):
        if not run_id or not self.goals._has_table(conn, "managed_session_links"):
            return None
        row = conn.execute(
            "SELECT r.run_id,r.status,r.updated_at FROM agent_runs r "
            "JOIN agent_sessions s ON s.session_id=r.session_id "
            "WHERE r.run_id=? AND s.kind='ordinary' AND s.status!='deleted' "
            "AND json_extract(r.record_payload,'$.parent_run_id') IS NULL "
            "AND EXISTS (SELECT 1 FROM managed_session_links l WHERE l.run_id=r.run_id "
            "AND l.task_session_id=r.session_id AND l.manager_session_id=?)",
            (run_id, session_id)).fetchone()
        return dict(row) if row else None

    def _card(self, conn, session_id, row, now):
        # Both readers use the supplied transaction and retain source checks.
        goal = self.goals._project(conn, row)
        matter = self.goals._bound_matter(conn, goal["goal_id"])
        objective = goal["objective"]
        preview = objective[:1200]
        card = {"kind": "goal", "goal_id": goal["goal_id"], "revision": goal["revision"], "status": goal["status"],
                "objective": preview, "objective_truncated": len(preview) < len(objective),
                "source_snapshot_at": now.isoformat(), "next_wake_at": goal.get("next_wake_at"),
                "attention_reasons": [], "score": 0}
        if card["objective_truncated"]:
            card["continuation"] = {"tool": "management.read_goal",
                                    "input": {"goal_id": goal["goal_id"], "start_char": len(preview)}}

        def reason(name, score):
            card["attention_reasons"].append(name)
            card["score"] += score

        if matter is not None:
            self._matter_fields(conn, card, matter, now)
        task = self._task(conn, session_id, goal.get("wait_run_id"))
        if task is not None:
            card["wait_run"] = task
            if task["status"] in {"failed", "timed_out"}:
                reason("owned_task_" + task["status"], 100)
            elif task["status"] in {"waiting_user", "waiting_confirmation"}:
                reason("owned_task_" + task["status"], 90)
            elif task["status"] == "completed":
                reason("owned_task_terminal_needs_business_review", 45)
        wake = self._time(goal.get("next_wake_at"))
        if wake is not None and wake <= now:
            reason("explicit_check_due", 30)
        updated = self._time(goal.get("updated_at"))
        if updated is not None and now - updated >= timedelta(days=7):
            reason("goal_record_not_updated_for_seven_days", 15)
        self._finish(card)
        return card

    def _matter_fields(self, conn, card, matter, now):
        def reason(name, score):
            card["attention_reasons"].append(name)
            card["score"] += score

        card["matter"] = {"matter_id": matter.matter_id, "revision": matter.revision,
                          "title": matter.title[:100], "summary": matter.summary[:140],
                          "status": matter.status, "priority": matter.priority, "due_at": matter.due_at}
        if matter.priority in {"high", "urgent"}:
            reason("matter_priority_" + matter.priority, 20 if matter.priority == "high" else 35)
        if matter.status == "waiting":
            reason("matter_waiting", 10)
        due = matter.due_at
        if due:
            # Date-only deadlines lack a timezone: retain date precision and
            # compare UTC dates rather than inventing an exact deadline.
            if len(due) == 10:
                day = date.fromisoformat(due)
                overdue = day < now.date()
                soon = day <= now.date() + timedelta(days=1)
            else:
                deadline = self._time(due)
                overdue = deadline is not None and deadline < now
                soon = deadline is not None and deadline <= now + timedelta(days=1)
            if overdue:
                reason("matter_deadline_overdue", 80)
            elif soon:
                reason("matter_deadline_within_one_day", 55)
        work = self.goals.work_service
        if callable(getattr(work, "_sidecar", None)):
            sidecar = work._sidecar(conn, matter.matter_id)
            if sidecar:
                management = {}
                for key, limit in (("next_action", 400), ("responsible_party", 30), ("management_state", 30)):
                    if isinstance(sidecar.get(key), str):
                        management[key] = sidecar[key][:limit]
                        if len(sidecar[key]) > limit:
                            management["truncated"] = True
                wait = sidecar.get("wait_condition")
                if isinstance(wait, dict):
                    management["wait_condition"] = {key: wait[key][:limit] for key, limit in
                        (("type", 30), ("description", 300), ("until", 80)) if isinstance(wait.get(key), str)}
                    if any(isinstance(wait.get(key), str) and len(wait[key]) > limit for key, limit in
                           (("type", 30), ("description", 300), ("until", 80))):
                        management["truncated"] = True
                if management.get("truncated"):
                    management["continuation"] = {"tool": "management.inspect_work", "input": {"matter_id": matter.matter_id}}
                card["management"] = management

    def _matter_card(self, conn, matter, now):
        objective = matter.title + ("\n" + matter.summary if matter.summary and matter.summary != matter.title else "")
        card = {"kind": "matter", "matter_id": matter.matter_id, "revision": matter.revision,
                "status": matter.status, "objective": objective[:1200],
                "objective_truncated": len(objective) > 1200,
                "source_snapshot_at": now.isoformat(), "attention_reasons": [], "score": 0}
        if card["objective_truncated"]:
            card["continuation"] = {"tool": "management.inspect_work", "input": {"matter_id": matter.matter_id}}
        self._matter_fields(conn, card, matter, now)
        self._finish(card)
        return card

    @staticmethod
    def _finish(card):
        if not card["attention_reasons"]:
            card["attention_reasons"].append("active_responsibility")
        card["severity"] = "high" if card["score"] >= 80 else "medium" if card["score"] >= 30 else "normal"

    def build(self, conn, session_id, goal_revisions: dict, now=None, matter_revisions=None):
        """Project frozen refs only; ranking never grants source or task authority."""
        instant = _instant(now)
        self.goals._manager(conn, session_id)
        cards = []
        matter_revisions = matter_revisions or {}
        reference_count = len(goal_revisions) + len(matter_revisions)
        # Only selected event references, never a global Matter scan.
        if reference_count > 16:
            raise ValueError("Attention context accepts at most 16 frozen references")
        for goal_id, revision in sorted(goal_revisions.items()):
            row = conn.execute("SELECT * FROM manager_goals WHERE session_id=? AND goal_id=? AND revision=? "
                               "AND status IN ('active','waiting')", (session_id, goal_id, revision)).fetchone()
            if row is None:
                continue
            try:
                if not self.goals.enabled_in_transaction(conn, goal_id):
                    continue
                cards.append(self._card(conn, session_id, row, instant))
            except (KeyError, PermissionError, ManagementConflictError):
                # No title/objective/reader handle from inaccessible bound data.
                continue
        bound = {card["matter"]["matter_id"]: card["matter"]["revision"] for card in cards if "matter" in card}
        work = self.goals.work_service
        for matter_id, revision in sorted(matter_revisions.items()):
            if bound.get(matter_id) == revision:
                reference_count -= 1
                continue
            if work is None:
                continue
            try:
                matter = work.matters.get_matter(matter_id=matter_id, conn=conn)
                if (matter.revision != revision or matter.status in {"done", "cancelled"}
                        or matter.deleted_at is not None):
                    continue
                cards.append(self._matter_card(conn, matter, instant))
            except (KeyError, PermissionError, ManagementConflictError):
                continue
        cards.sort(key=lambda card: (-card["score"], card["kind"], card.get("goal_id", card.get("matter_id"))))
        selected = cards[:self.max_items]
        note = ("Source snapshot only; current freshness and business completion are not established. "
                "Record age is not proof of missing progress. Date-only deadlines use UTC date comparison.")

        def payload(items):
            omitted = cards[len(items):]
            refs = []
            for card in omitted[:8]:
                is_goal = card["kind"] == "goal"
                key = "goal_id" if is_goal else "matter_id"
                args = {key: card[key], **({"start_char": 0} if is_goal else {})}
                refs.append({"kind": card["kind"], key: card[key], "revision": card["revision"],
                             "reader": {"tool": "management.read_goal" if is_goal else "management.inspect_work", "input": args}})
            return {"items": items, "omitted_count": reference_count - len(items),
                    "omitted_refs": refs,
                    "omitted_refs_truncated": len(omitted) > 8,
                    "source_snapshot_at": instant.isoformat(), "note": note}

        result = payload(selected)
        while self._size(result) > self.max_chars:
            if result["omitted_refs"]:
                result["omitted_refs"].pop()
                result["omitted_refs_truncated"] = True
            elif len(selected) == 1 and not selected[0].get("details_truncated"):
                card = selected[0]
                compact = {key: card[key] for key in (
                    "kind", "goal_id", "matter_id", "revision", "status", "source_snapshot_at",
                    "attention_reasons", "score", "severity",
                ) if key in card}
                compact.update(objective=card["objective"][:64], details_truncated=True,
                               objective_truncated=(card["objective_truncated"] or len(card["objective"]) > 64))
                is_goal = card["kind"] == "goal"
                key = "goal_id" if is_goal else "matter_id"
                if compact["objective_truncated"]:
                    compact["continuation"] = {
                        "tool": "management.read_goal" if is_goal else "management.inspect_work",
                        "input": {key: card[key], **({"start_char": len(compact["objective"])} if is_goal else {})},
                    }
                if card.get("matter"):
                    compact["details_reader"] = {"tool": "management.inspect_work",
                                                  "input": {"matter_id": card["matter"]["matter_id"]}}
                selected = [compact]
                result = payload(selected)
            elif selected:
                selected = selected[:-1]
                result = payload(selected)
            else:
                break
        if self._size(result) <= self.max_chars:
            return result
        for minimal in ({"items": [], "omitted_count": reference_count, "note": "Read authorized attention sources separately."},
                        {"items": [], "omitted_count": reference_count}, {}):
            if self._size(minimal) <= self.max_chars:
                return minimal
        return {}
