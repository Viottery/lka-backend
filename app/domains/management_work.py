"""Bounded deterministic Matter organization with deferred autonomous publication."""
from __future__ import annotations

import json
from contextlib import closing
from datetime import UTC, datetime
from hashlib import sha256
from types import SimpleNamespace
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.domains.management import ManagementConflictError
from app.domains.matters import MatterCreateInput, MatterUpdateInput
from app.storage.commit_signals import notify_after_commit


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class WaitCondition(StrictModel):
    type: Literal["time", "user", "external", "work"]
    description: str = Field(default="", max_length=1000)
    until: str | None = Field(default=None, max_length=80)
    reference: str | None = Field(default=None, max_length=200)


class WorkManagement(StrictModel):
    purpose: Literal["goal", "action", "watch"] = "action"
    responsible_party: Literal["user", "agent", "external"] = "agent"
    next_action: str = Field(default="", max_length=2000)
    wait_condition: WaitCondition | None = None
    management_state: Literal["active", "paused"] = "active"


class WorkSource(StrictModel):
    source_type: str = Field(min_length=1, max_length=128)
    source_id: str = Field(min_length=1, max_length=256)
    reason: str = Field(default="", max_length=2000)


class WorkChanges(StrictModel):
    title: str | None = Field(default=None, max_length=1000)
    summary: str | None = Field(default=None, max_length=16000)
    parent_matter_id: str | None = Field(default=None, max_length=256)
    project_id: str | None = Field(default=None, max_length=256)
    status: Literal["open", "in_progress", "waiting", "done", "cancelled"] | None = None
    priority: Literal["low", "normal", "high", "urgent"] | None = None
    due_at: str | None = Field(default=None, max_length=80)
    tags: list[str] | None = Field(default=None, max_length=50)
    visibility: Literal["user", "internal"] = "user"
    scheduled_on: str | None = Field(default=None, max_length=80)
    scheduled_start: str | None = Field(default=None, max_length=80,
        description="Time block start; supply with scheduled_end, or clear both with null. Never invent a duration.")
    scheduled_end: str | None = Field(default=None, max_length=80,
        description="Time block end, after scheduled_start; supply or clear both endpoints together.")
    inbox: bool = False
    section: str = Field(default="", max_length=120)
    sort_order: float = Field(default=0, allow_inf_nan=False)
    sections: list[str] = Field(default_factory=list, max_length=100)


class WorkFields(WorkChanges):
    title: str = Field(min_length=1, max_length=1000)
    summary: str = Field(default="", max_length=16000)
    kind: Literal["task", "plan"] = "task"
    status: Literal["open", "in_progress", "waiting", "done", "cancelled"] = "open"
    priority: Literal["low", "normal", "high", "urgent"] = "normal"
    tags: list[str] = Field(default_factory=list, max_length=50)
    visibility: Literal["user", "internal"] = "internal"
    source_links: list[WorkSource] = Field(default_factory=list, max_length=50)


class CreateWork(StrictModel):
    op: Literal["create"]
    ref: str = Field(min_length=1, max_length=128)
    fields: WorkFields
    management: WorkManagement | None = None


class UpdateWork(StrictModel):
    op: Literal["update"]
    matter_id: str = Field(min_length=1, max_length=256)
    expected_revision: int = Field(ge=1)
    changes: WorkChanges
    management: WorkManagement | None = None


class LinkWork(StrictModel):
    op: Literal["link"]
    source: str = Field(min_length=1, max_length=256)
    target: str = Field(min_length=1, max_length=256)
    relation: Literal["supports", "depends_on"]


class BindGoal(StrictModel):
    op: Literal["bind_goal"]
    goal_id: str = Field(min_length=1, max_length=256)
    matter_id: str = Field(min_length=1, max_length=256)
    expected_goal_revision: int = Field(ge=1)
    expected_matter_revision: int = Field(ge=1)


WorkOperation = Annotated[CreateWork | UpdateWork | LinkWork | BindGoal, Field(discriminator="op")]


class OrganizeWorkInput(StrictModel):
    command_id: str = Field(min_length=1, max_length=128)
    operations: list[WorkOperation] = Field(min_length=1, max_length=20)


class ManagementWorkService:
    def __init__(self, conn_factory, *, matters, tasks, management, goals, run_store, run_manager):
        self._conn = conn_factory
        self.matters, self.tasks, self.management, self.goals = matters, tasks, management, goals
        self.run_store, self.run_manager = run_store, run_manager

    def ensure_schema(self):
        with closing(self._conn()) as conn, conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS matter_management (
                    matter_id TEXT PRIMARY KEY, manager_session_id TEXT NOT NULL,
                    purpose TEXT NOT NULL, responsible_party TEXT NOT NULL,
                    next_action TEXT NOT NULL, wait_condition TEXT,
                    management_state TEXT NOT NULL, revision INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS manager_work_relations (
                    manager_session_id TEXT NOT NULL, source_matter_id TEXT NOT NULL,
                    target_matter_id TEXT NOT NULL, relation TEXT NOT NULL,
                    PRIMARY KEY(manager_session_id,source_matter_id,target_matter_id,relation)
                );
                CREATE TABLE IF NOT EXISTS manager_goal_matter_links (
                    goal_id TEXT PRIMARY KEY, manager_session_id TEXT NOT NULL, matter_id TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS manager_work_proposals (
                    execution_id TEXT NOT NULL, command_id TEXT NOT NULL,
                    manager_session_id TEXT NOT NULL, payload TEXT NOT NULL,
                    PRIMARY KEY(execution_id,command_id)
                );
            """)

    def check_run(self, run_id, conn=None):
        """Read-only business fence; safe to call from the coordinator guard."""
        if conn is None:
            with closing(self._conn()) as db:
                return self.check_run(run_id, conn=db)
        row = conn.execute("SELECT record_payload FROM agent_runs WHERE run_id=?", (run_id,)).fetchone()
        if row is None:
            raise ManagementConflictError("Management run is unavailable")
        record = json.loads(row[0])
        versions = record.get("metadata", {}).get("manager_matter_versions", {})
        for matter_id, revision in versions.items():
            try:
                matter = self.matters.get_matter(matter_id=matter_id, conn=conn)
            except KeyError as exc:
                raise ManagementConflictError("Bound Matter is unavailable") from exc
            sidecar = self._sidecar(conn, matter_id)
            if matter.revision != revision or (sidecar and sidecar["management_state"] == "paused"):
                raise ManagementConflictError("Bound Matter changed or management is paused")

    @staticmethod
    def _safe_matter(record, *, preview=False):
        item = record.model_dump(mode="json")
        item.pop("metadata", None)
        if preview:
            summary = item["summary"]
            item["summary"] = summary[:500]
            item["title"] = item["title"][:500]
            links = item["source_links"]
            item["source_link_count"] = len(links)
            item["source_links"] = [{**link, "reason": link["reason"][:200]} for link in links[:5]]
            item["source_links_truncated"] = len(links) > 5 or any(len(link["reason"]) > 200 for link in links[:5])
            item["read_more"] = {"matter_id": item["matter_id"]} if len(summary) > 500 or item["source_links_truncated"] else None
        return item

    @staticmethod
    def _sidecar(conn, matter_id):
        row = conn.execute("SELECT * FROM matter_management WHERE matter_id=?", (matter_id,)).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["wait_condition"] = json.loads(result["wait_condition"]) if result["wait_condition"] else None
        return result

    def overview(self, context, limit=30, offset=0, visibility="all"):
        self.tasks.caller(context)
        self.tasks._page(limit, offset)
        if visibility not in {"user", "internal", "all"}:
            raise ValueError("Invalid visibility")
        with closing(self._conn()) as conn:
            conn.execute("BEGIN")
            self.tasks.caller(context, conn=conn)
            page = self.matters.list_matters(limit=limit, offset=offset, visibility=visibility, conn=conn)
            items = []
            for record in page.matters:
                item = self._safe_matter(record, preview=True)
                item["management"] = self._sidecar(conn, record.matter_id)
                row = conn.execute("SELECT r.record_payload FROM manager_goal_matter_links l "
                                   "JOIN manager_goals g ON g.goal_id=l.goal_id "
                                   "JOIN agent_runs r ON r.run_id=g.wait_run_id "
                                   "WHERE l.matter_id=? AND l.manager_session_id=? "
                                   "ORDER BY r.created_at DESC,r.run_id DESC LIMIT 1",
                                   (record.matter_id, context.session_id)).fetchone()
                item["latest_run"] = self.tasks._run_view(json.loads(row[0])) if row else None
                items.append(item)
            return {"matters": items, "total": page.total_count, "next_offset": page.next_offset}

    def inspect_work(self, context, matter_id):
        self.tasks.caller(context)
        with closing(self._conn()) as conn:
            conn.execute("BEGIN")
            self.tasks.caller(context, conn=conn)
            record = self.matters.get_matter(matter_id=matter_id, conn=conn)
            relations = []
            for row in conn.execute("SELECT * FROM manager_work_relations WHERE manager_session_id=? "
                                    "AND (source_matter_id=? OR target_matter_id=?) LIMIT 101",
                                    (context.session_id, matter_id, matter_id)):
                # Related IDs are disclosed only after the normal Matter scope checks.
                self.matters.get_matter(matter_id=row["source_matter_id"], conn=conn)
                self.matters.get_matter(matter_id=row["target_matter_id"], conn=conn)
                relations.append(dict(row))
            goals = [dict(row) for row in conn.execute(
                "SELECT g.goal_id,g.revision FROM manager_goal_matter_links l JOIN manager_goals g "
                "ON g.goal_id=l.goal_id WHERE l.manager_session_id=? AND l.matter_id=? LIMIT 100",
                (context.session_id, matter_id))]
            return {"matter": self._safe_matter(record), "management": self._sidecar(conn, matter_id),
                    "relations": relations[:100], "relations_truncated": len(relations) > 100, "linked_goals": goals}

    @staticmethod
    def _resolve(value, refs):
        return refs.get(value, value)

    def _manage(self, conn, session_id, matter_id, values):
        prior = self._sidecar(conn, matter_id)
        if prior and prior["manager_session_id"] != session_id:
            raise ManagementConflictError("Matter is already managed by another management session")
        revision = prior["revision"] + 1 if prior else 1
        defaults = WorkManagement().model_dump(mode="json")
        values = {**defaults, **({key: prior[key] for key in defaults} if prior else {}), **values}
        conn.execute("INSERT INTO matter_management VALUES(?,?,?,?,?,?,?,?) "
                     "ON CONFLICT(matter_id) DO UPDATE SET purpose=excluded.purpose,"
                     "responsible_party=excluded.responsible_party,next_action=excluded.next_action,"
                     "wait_condition=excluded.wait_condition,management_state=excluded.management_state,revision=excluded.revision",
                     (matter_id, session_id, values["purpose"], values["responsible_party"], values["next_action"],
                      _json(values["wait_condition"]) if values.get("wait_condition") else None,
                      values["management_state"], revision))

    def _link(self, conn, session_id, source, target, relation):
        self.matters.get_matter(matter_id=source, conn=conn)
        self.matters.get_matter(matter_id=target, conn=conn)
        if source == target:
            raise ValueError("Work relationships cannot reference themselves")
        # Both relations must remain acyclic; mixed edges cannot hide a cycle.
        cycle = conn.execute("WITH RECURSIVE reachable(id) AS (SELECT ? UNION "
                             "SELECT r.target_matter_id FROM manager_work_relations r JOIN reachable p "
                             "ON r.source_matter_id=p.id) SELECT 1 FROM reachable WHERE id=? LIMIT 1",
                             (target, source)).fetchone()
        if cycle:
            raise ValueError("Work relationship would create a cycle")
        conn.execute("INSERT OR IGNORE INTO manager_work_relations VALUES(?,?,?,?)", (session_id, source, target, relation))

    def _apply(self, conn, session_id, payload):
        refs = payload["refs"]
        results = []
        for operation in payload["operations"]:
            op = operation["op"]
            if op == "create":
                fields = dict(operation["fields"])
                if fields.get("parent_matter_id"):
                    fields["parent_matter_id"] = self._resolve(fields["parent_matter_id"], refs)
                record = self.matters.create_matter(MatterCreateInput.model_validate(fields), conn=conn,
                                                    matter_id=refs[operation["ref"]])
            elif op == "update":
                matter_id = self._resolve(operation["matter_id"], refs)
                current = self.matters.get_matter(matter_id=matter_id, conn=conn)
                if current.revision != operation["expected_revision"]:
                    raise ManagementConflictError("Matter revision changed")
                changes = dict(operation["changes"])
                if changes.get("parent_matter_id"):
                    changes["parent_matter_id"] = self._resolve(changes["parent_matter_id"], refs)
                record = self.matters.update_matter(matter_id=matter_id, conn=conn,
                    payload=MatterUpdateInput.model_validate({**changes, "expected_revision": operation["expected_revision"]}))
            elif op == "link":
                source, target = (self._resolve(operation[key], refs) for key in ("source", "target"))
                self._link(conn, session_id, source, target, operation["relation"])
                results.append({"op": op, "source": source, "target": target, "relation": operation["relation"]})
                continue
            else:
                matter_id = self._resolve(operation["matter_id"], refs)
                record = self.matters.get_matter(matter_id=matter_id, conn=conn)
                goal = conn.execute("SELECT * FROM manager_goals WHERE goal_id=? AND session_id=?",
                                    (operation["goal_id"], session_id)).fetchone()
                if goal is None:
                    raise ValueError("Goal must belong to this management session")
                if goal["revision"] != operation["expected_goal_revision"] or record.revision != operation["expected_matter_revision"]:
                    raise ManagementConflictError("Goal or Matter revision changed")
                old = conn.execute("SELECT * FROM manager_goal_matter_links WHERE goal_id=?", (goal["goal_id"],)).fetchone()
                if old and (old["matter_id"] != matter_id or old["manager_session_id"] != session_id):
                    raise ManagementConflictError("Goal is already bound to another Matter")
                conn.execute("INSERT OR IGNORE INTO manager_goal_matter_links VALUES(?,?,?)", (goal["goal_id"], session_id, matter_id))
                results.append({"op": op, "goal_id": goal["goal_id"], "matter_id": matter_id})
                continue
            if operation.get("management") is not None:
                self._manage(conn, session_id, record.matter_id, operation["management"])
            results.append({"op": op, "matter_id": record.matter_id, "revision": record.revision})
        return results

    def _execution(self, conn, execution_id, session_id, run_id=None, *, authority_checked=False):
        row = conn.execute("SELECT * FROM manager_executions WHERE execution_id=? AND session_id=?", (execution_id, session_id)).fetchone()
        if row is None or row["status"] != "bound" or (run_id and row["run_id"] != run_id):
            raise ManagementConflictError("Management execution is no longer current")
        if not authority_checked:
            self.tasks.caller(SimpleNamespace(run_id=row["run_id"], session_id=session_id), mutate=True, conn=conn)
        return row

    @notify_after_commit
    def create_matter(self, context, *, command_id, title, summary="", **fields):
        """Compact capture entry using the existing proposal/publication path."""
        self.tasks.caller(context, mutate=True, supervisor_only=True)
        return self.organize_work(context, command_id=command_id, operations=[{
            "op": "create", "ref": "matter", "fields": {
                "title": title, "summary": summary, "visibility": "user", **fields,
            },
        }])

    @notify_after_commit
    def update_matter(self, context, *, command_id, matter_id, expected_revision, changes):
        self.tasks.caller(context, mutate=True, supervisor_only=True)
        if not isinstance(changes, dict) or not changes:
            raise ValueError("changes must contain at least one field")
        return self.organize_work(context, command_id=command_id, operations=[{
            "op": "update", "matter_id": matter_id, "expected_revision": expected_revision,
            "changes": changes,
        }])

    @notify_after_commit
    def organize_work(self, context, command_id, operations):
        args = OrganizeWorkInput.model_validate({"command_id": command_id, "operations": operations})
        normalized = args.model_dump(mode="json", exclude_unset=True)["operations"]
        # Defaults on complete create/management objects are intentional; updates retain only supplied fields.
        for source, operation in zip(args.operations, normalized, strict=True):
            if isinstance(source, CreateWork):
                operation["fields"] = source.fields.model_dump(mode="json", exclude_unset=True)
                operation["fields"].setdefault("visibility", "internal")
            if getattr(source, "management", None) is not None:
                operation["management"] = source.management.model_dump(mode="json", exclude_unset=True)
        if len(_json(normalized).encode()) > 64000:
            raise ValueError("Work command must not exceed 64000 bytes")
        with closing(self._conn()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            run = self.tasks.caller(context, mutate=True, conn=conn)
            fingerprint, prior = self.tasks._command(conn, context, command_id, "organize_work", {"operations": normalized})
            execution_id = run.metadata.get("manager_execution_id")
            if prior:
                if prior.get("status") == "proposed" and prior.get("execution_id") != execution_id:
                    raise ManagementConflictError("Prior work proposal belongs to another execution; inspect work and use a new command_id")
                if prior.get("status") == "proposed":
                    self._execution(conn, execution_id, context.session_id, run.run_id)
                return prior
            refs = {}
            for operation in normalized:
                if operation["op"] == "create":
                    ref = operation["ref"]
                    if ref in refs:
                        raise ValueError("Create refs must be unique within the command")
                    refs[ref] = "matter_" + sha256(_json([context.session_id, command_id, ref]).encode()).hexdigest()[:32]
            payload = {"operations": normalized, "refs": refs}
            if execution_id:
                self._execution(conn, execution_id, context.session_id, run.run_id)
                conn.execute("SAVEPOINT work_validation")
                try:
                    # Finalization applies goal proposals first; validation must see the same state.
                    self.tasks.apply_goal_proposals(conn, execution_id, context.session_id)
                    for row in conn.execute("SELECT payload FROM manager_work_proposals WHERE execution_id=? ORDER BY rowid", (execution_id,)).fetchall():
                        self._apply(conn, context.session_id, json.loads(row[0]))
                    results = self._apply(conn, context.session_id, payload)
                finally:
                    conn.execute("ROLLBACK TO work_validation")
                    conn.execute("RELEASE work_validation")
                conn.execute("INSERT INTO manager_work_proposals VALUES(?,?,?,?)", (execution_id, command_id, context.session_id, _json(payload)))
            else:
                results = self._apply(conn, context.session_id, payload)
            receipt = {"command_id": command_id, "action": "organize_work", "status": "proposed" if execution_id else "applied",
                       "execution_id": execution_id, "refs": refs, "results": results}
            # A bounded validation may still outlive a dispatch lease. Fence again before commit.
            self.tasks.caller(context, mutate=True, conn=conn)
            conn.execute("INSERT INTO management_commands VALUES(?,?,?,?,?,?)",
                         (context.session_id, command_id, fingerprint, "organize_work", _json(receipt), datetime.now(UTC).isoformat()))
            return receipt

    def apply_proposals(self, conn, execution_id, session_id, *, authority_checked=False):
        self._execution(conn, execution_id, session_id, authority_checked=authority_checked)
        for row in conn.execute("SELECT command_id,payload FROM manager_work_proposals WHERE execution_id=? "
                                "AND manager_session_id=? ORDER BY rowid", (execution_id, session_id)).fetchall():
            command = conn.execute("SELECT receipt FROM management_commands WHERE manager_session_id=? AND command_id=?",
                                   (session_id, row["command_id"])).fetchone()
            receipt = json.loads(command[0])
            if receipt["status"] == "applied":
                continue
            receipt["results"] = self._apply(conn, session_id, json.loads(row["payload"]))
            receipt["status"] = "applied"
            conn.execute("UPDATE management_commands SET receipt=? WHERE manager_session_id=? AND command_id=?",
                         (_json(receipt), session_id, row["command_id"]))
