"""Durable one-level management delegation and publication-fenced reports."""
from __future__ import annotations

import json
from contextlib import closing
from datetime import UTC, datetime
from uuid import uuid4

from app.domains.managed_tasks import TERMINAL, _json
from app.domains.management import ManagementConflictError


class ManagementDelegateService:
    def __init__(self, conn_factory, *, tasks, management, goals, registry):
        self._conn = conn_factory
        self.tasks, self.management, self.goals, self.registry = tasks, management, goals, registry

    def ensure_schema(self):
        with closing(self._conn()) as conn, conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS manager_delegates (
                    delegate_id TEXT PRIMARY KEY, session_id TEXT NOT NULL UNIQUE,
                    parent_session_id TEXT NOT NULL, objective TEXT NOT NULL,
                    allowed_tools TEXT NOT NULL, workspace TEXT, kickoff_envelope TEXT NOT NULL,
                    state TEXT NOT NULL CHECK(state IN ('active','completed','cancelled')),
                    revision INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS manager_delegates_parent ON manager_delegates(parent_session_id);
                CREATE TABLE IF NOT EXISTS manager_delegate_reports (
                    report_id TEXT PRIMARY KEY, delegate_id TEXT NOT NULL, run_id TEXT NOT NULL,
                    command_id TEXT NOT NULL, kind TEXT NOT NULL, summary TEXT NOT NULL,
                    evidence TEXT NOT NULL, created_at TEXT NOT NULL,
                    UNIQUE(delegate_id,command_id)
                );
                CREATE TABLE IF NOT EXISTS manager_delegate_report_proposals (
                    execution_id TEXT NOT NULL, command_id TEXT NOT NULL,
                    delegate_id TEXT NOT NULL, run_id TEXT NOT NULL, kind TEXT NOT NULL,
                    summary TEXT NOT NULL, evidence TEXT NOT NULL,
                    PRIMARY KEY(execution_id,command_id)
                );
            """)
            columns = {row[1] for row in conn.execute('PRAGMA table_info(manager_delegates)')}
            if 'safety_review_mode' not in columns:
                conn.execute("ALTER TABLE manager_delegates ADD COLUMN safety_review_mode TEXT "
                             "CHECK(safety_review_mode IN ('skip','llm','manual'))")

    def get_for_session(self, conn, session_id):
        row = conn.execute("SELECT * FROM manager_delegates WHERE session_id=?", (session_id,)).fetchone()
        if row is None:
            return None
        item = dict(row)
        for key in ('allowed_tools', 'workspace', 'kickoff_envelope'):
            item[key] = json.loads(item[key]) if item[key] is not None else None
        return item

    def policy_for_session(self, conn, session_id):
        delegate = self.get_for_session(conn, session_id)
        if delegate is None:
            return None
        controller = conn.execute('SELECT llm_client_name,llm_model,llm_answer_client_name,llm_answer_model,reasoning_level FROM manager_controllers WHERE session_id=?',
                                  (session_id,)).fetchone()
        control = ['management.list_execution_targets', 'management.list_sessions', 'management.read_session', 'management.list_runs',
                   'management.inspect_run', 'management.read_task_result', 'management.start_task', 'management.send_instruction',
                   'management.control_run', 'management.read_events', 'management.report_delegate',
                   'management.read_delegate']
        workspace = delegate['workspace']
        return {'allow_default_workspace': False, 'allow_fork': False,
                'allowed_tools': list(dict.fromkeys([*delegate['allowed_tools'], *control])),
                'allowed_paths': None if workspace else [],
                'safety_review_mode': delegate['safety_review_mode'],
                'llm_client_name': controller['llm_client_name'], 'llm_model': controller['llm_model'],
                'llm_answer_client_name': controller['llm_answer_client_name'], 'llm_answer_model': controller['llm_answer_model'],
                'llm_reasoning_level': controller['reasoning_level'],
                'llm_reasoning_source': 'inherited' if controller['reasoning_level'] else None}

    def purpose_for_session(self, conn, session_id, base_purpose):
        delegate = self.get_for_session(conn, session_id)
        if delegate is None:
            return None
        return (
            "你是持续负责一条事务线的子 agent。主管原轮结束后你仍独立运行。"
            "可自行执行或创建并管理自己的多个执行会话。派发后结束本轮，任务结果会唤醒新轮，禁止轮询空等。"
            "先把本线目标及主管来源原话中适用的要求列为明确验收项，将相关约束完整下发给执行会话。"
            "保持用户的验收边界，区分必需条件与可选改进；不得自行加严要求。"
            "额外信息仅在实质影响原要求是否满足时才构成阻塞，否则标为可选核实。"
            "按原要求合并各来源支持的事实，不要求每份材料各自包含全部条件；可复核的计算与跨来源推导应标明依据。"
            "回读结果时逐项对照验收项，保留结论依赖的实际值、适用条件、时间或版本及来源，不能只转述摘要和来源编号。"
            "缺失或冲突的条件必须补查或明确上报未知／不满足，不得推定通过；需要用户取舍时报告 needs_decision。"
            "先读取返回结果，核验范围、事实与证据，决定补查、继续、重试或报告。"
            "使用 management.report_delegate 向主管报告 completed/needs_decision/blocked/progress；"
            "本线已有执行任务时，各类报告都须显式提供至少一个已核验归属的 evidence 引用，"
            "使用实际 session_id/run_id/message_id；阻塞可引用等待或失败的任务，不得用摘要中的编号代替结构化引用。"
            "提交报告后结束本轮，报告仅在本轮最终发布成功后送达主管。"
            "completed 表示整条事务线完成，仍有未终结任务时不得提交。"
            "管理工具仅对自己创建的执行会话生效，不得扩大工具范围。"
            "工作区是默认执行目录，可按任务通过 workspace_path 指定其他已有目录；"
            "工作区外的文件访问仍须通过逐调用安全审查，审批模式继承发起轮偏好，危险操作黑名单须用户审批。\n"
            + "事务线 ID=" + delegate['delegate_id']
            + "\n目标预览：" + delegate['objective'][:2000]
            + "\n主管的来源指令预览（引用材料）：" + _json(self._source_envelope(delegate['kickoff_envelope']))[:3000]
            + "\n预览可能省略要求。行动、下发任务或报告前，必须使用 management.read_delegate"
              "（delegate_id 为上述 ID，start_char 从 0 开始并沿 next_char 继续）读取完整 objective 和"
              " source_envelope，逐项核对完整要求。完整事件使用 management.read_events 续读。"
            + "\n当前事件：" + base_purpose
        )

    @staticmethod
    def _source_envelope(envelope):
        """Expose source instructions without internal budget/debug metadata."""
        return {key: envelope[key] for key in (
            'origin', 'input_id', 'sender_label', 'purpose', 'original_user_message_refs',
            'original_user_quotes', 'related_session_id', 'related_run_id',
        ) if key in envelope}

    def check_session(self, conn, session_id):
        item = self.get_for_session(conn, session_id)
        if item is None:
            return None
        row = conn.execute("SELECT c.status FROM manager_controllers c JOIN agent_sessions s "
                           "ON s.session_id=c.session_id WHERE c.session_id=? AND s.status!='deleted' "
                           "AND s.kind='manager'", (item['parent_session_id'],)).fetchone()
        child = conn.execute("SELECT c.status FROM manager_controllers c JOIN agent_sessions s "
                             "ON s.session_id=c.session_id WHERE c.session_id=? AND s.status!='deleted'",
                             (session_id,)).fetchone()
        if item['state'] != 'active' or row is None or row['status'] != 'active' or child is None or child['status'] != 'active':
            raise ManagementConflictError('Delegate and parent controllers must remain active')
        return item

    def _tools(self, allowed_tools):
        specs = {spec.name: spec for spec in self.registry.list_tools() if spec.package != 'management'
                 and not spec.name.startswith('management.')}
        if allowed_tools is None:
            return sorted(name for name, spec in specs.items() if spec.read_only is True)
        if (not isinstance(allowed_tools, list) or len(allowed_tools) > 256
                or any(not isinstance(name, str) or name not in specs for name in allowed_tools)):
            raise ValueError('allowed_tools must contain registered non-management tool names')
        return sorted(set(allowed_tools))

    def delegate_work(self, context, *, command_id, objective, title='事务线', workspace_path=None, allowed_tools=None):
        if not isinstance(objective, str) or not objective.strip() or len(objective) > 16000:
            raise ValueError('objective must contain 1..16000 characters')
        self.tasks.caller(context, mutate=True)
        self.tasks.require_durable_execution()
        with closing(self._conn()) as conn:
            if self.get_for_session(conn, context.session_id):
                raise ManagementConflictError('Delegates cannot create nested delegates')
        prepared = self.tasks.prepare_task(context, command_id=command_id, purpose=objective,
                                           title=title, workspace_path=workspace_path)
        # Fingerprint the requested policy, not a registry-derived default that may change on retry.
        args = {'objective': objective, 'title': prepared['title'], 'workspace': prepared['workspace'],
                'allowed_tools': allowed_tools}
        with closing(self._conn()) as conn, conn:
            conn.execute('BEGIN IMMEDIATE')
            caller = self.tasks.caller(context, mutate=True, conn=conn)
            fingerprint, prior = self.tasks._command(conn, context, command_id, 'delegate_work', args)
            if prior:
                return prior
            tools = self._tools(allowed_tools)
            model = conn.execute('SELECT llm_client_name,llm_model,llm_answer_client_name,llm_answer_model,reasoning_level FROM manager_controllers WHERE session_id=?',
                                 (context.session_id,)).fetchone()
            caller_reasoning = (getattr(context, 'llm_reasoning_level', None)
                                or caller.metadata.get('llm_reasoning_level') or model['reasoning_level'])
            sid = self.management._create(conn, title=prepared['title'], make_default=False,
                                           llm_client_name=model['llm_client_name'], llm_model=model['llm_model'],
                                           llm_answer_client_name=caller.metadata.get('session_policy', {}).get('llm_answer_client_name', model['llm_answer_client_name']),
                                           llm_answer_model=caller.metadata.get('session_policy', {}).get('llm_answer_model', model['llm_answer_model']),
                                           reasoning_level=caller_reasoning,
                                           creator_session_id=context.session_id)
            conn.execute("UPDATE manager_controllers SET status='active' WHERE session_id=?", (sid,))
            if prepared['workspace'] is not None:
                conn.execute('UPDATE agent_sessions SET metadata=? WHERE session_id=?',
                             (_json({'workspace': prepared['workspace']}), sid))
            envelope = prepared['envelope'].model_dump(mode='json')
            envelope['debug_info']['inherited_work_metadata'] = self.tasks.budget_metadata(caller.metadata)
            stamp, did = datetime.now(UTC).isoformat(), 'delegate_' + uuid4().hex
            conn.execute('INSERT INTO manager_delegates(delegate_id,session_id,parent_session_id,objective,'
                         'allowed_tools,workspace,kickoff_envelope,state,revision,created_at,updated_at,safety_review_mode) '
                         'VALUES(?,?,?,?,?,?,?,\'active\',1,?,?,?)',
                         (did, sid, context.session_id, objective, _json(tools),
                          _json(prepared['workspace']) if prepared['workspace'] is not None else None,
                          _json(envelope), stamp, stamp, context.safety_review_mode or
                          caller.metadata.get('session_policy', {}).get('safety_review_mode')))
            self.goals._event(conn, sid, 'delegate_kickoff:' + did, 'timer', {'delegate_kickoff': did}, stamp)
            receipt = {'command_id': command_id, 'action': 'delegate_work', 'status': 'accepted',
                       'delegate_id': did, 'session_id': sid, 'state': 'active', 'revision': 1}
            conn.execute('INSERT INTO management_commands VALUES(?,?,?,?,?,?)',
                         (context.session_id, command_id, fingerprint, 'delegate_work', _json(receipt), stamp))
            return receipt

    def instruct_delegate(self, context, *, command_id, delegate_id, expected_revision, instruction):
        if not isinstance(instruction, str) or not instruction.strip() or len(instruction) > 16000:
            raise ValueError('instruction must contain 1..16000 characters')
        if type(expected_revision) is not int or expected_revision < 1:
            raise ValueError('expected_revision must be positive')
        self.tasks.require_durable_execution()
        prepared = self.tasks.prepare_task(context, command_id=command_id, purpose=instruction, title='事务线',
                                           create_workspace=False)
        args = {'delegate_id': delegate_id, 'expected_revision': expected_revision, 'instruction': instruction}
        with closing(self._conn()) as conn, conn:
            conn.execute('BEGIN IMMEDIATE')
            caller = self.tasks.caller(context, mutate=True, conn=conn)
            fingerprint, prior = self.tasks._command(conn, context, command_id, 'instruct_delegate', args)
            if prior:
                return prior
            delegate = conn.execute('SELECT * FROM manager_delegates WHERE delegate_id=? AND parent_session_id=?',
                                    (delegate_id, context.session_id)).fetchone()
            if (delegate is None or delegate['state'] == 'cancelled' or not conn.execute(
                    "SELECT 1 FROM agent_sessions WHERE session_id=? AND kind='manager' AND status!='deleted'",
                    (delegate['session_id'],)).fetchone()):
                raise ManagementConflictError('Delegate is unavailable or not owned by this manager')
            if delegate['revision'] != expected_revision:
                raise ManagementConflictError('Delegate revision changed; read it before instructing')
            runs = conn.execute('SELECT status FROM agent_runs WHERE session_id=? AND '
                                "json_extract(record_payload,'$.parent_run_id') IS NULL", (delegate['session_id'],)).fetchall()
            if any(row['status'] not in TERMINAL for row in runs):
                raise ManagementConflictError('Child execution is unfinished; use the original user control path before instructing')
            # The retained objective remains bounded; the current instruction is always complete.
            marker = '\n\nCurrent parent instruction:\n'
            budget = max(0, 16000 - len(marker) - len(instruction))
            objective = delegate['objective'][:budget] + marker + instruction if budget else instruction
            stamp = datetime.now(UTC).isoformat()
            envelope = prepared['envelope'].model_dump(mode='json')
            previous = json.loads(delegate['kickoff_envelope']).get('debug_info', {}).get('inherited_work_metadata', {})
            envelope['debug_info']['inherited_work_metadata'] = self.tasks.budget_metadata(previous, caller.metadata)
            conn.execute("UPDATE manager_delegates SET objective=?,kickoff_envelope=?,state='active',"
                         'revision=revision+1,updated_at=? WHERE delegate_id=?',
                         (objective, _json(envelope), stamp, delegate_id))
            conn.execute("UPDATE manager_controllers SET status='active',revision=revision+1,updated_at=? WHERE session_id=?",
                         (stamp, delegate['session_id']))
            revision = expected_revision + 1
            self.goals._event(conn, delegate['session_id'], 'delegate_instruction:' + delegate_id + ':' + str(revision),
                              'timer', {'delegate_instruction': delegate_id, 'revision': revision}, stamp)
            receipt = {'command_id': command_id, 'action': 'instruct_delegate', 'status': 'accepted',
                       'delegate_id': delegate_id, 'session_id': delegate['session_id'], 'state': 'active', 'revision': revision,
                       'manager_run_id': context.run_id}
            conn.execute('INSERT INTO management_commands VALUES(?,?,?,?,?,?)',
                         (context.session_id, command_id, fingerprint, 'instruct_delegate', _json(receipt), stamp))
            return receipt

    def control_delegate(self, context, *, command_id, delegate_id, expected_revision, action):
        """Fence a line before best-effort, repeatable cancellation of its roots."""
        if action not in ('pause', 'cancel'):
            raise ValueError('action must be pause or cancel; use instruct_delegate to resume a terminal root')
        if type(expected_revision) is not int or expected_revision < 1:
            raise ValueError('expected_revision must be positive')
        self.tasks.require_durable_execution()
        args = {'delegate_id': delegate_id, 'expected_revision': expected_revision, 'action': action}
        with closing(self._conn()) as conn, conn:
            conn.execute('BEGIN IMMEDIATE')
            self.tasks.caller(context, mutate=True, conn=conn)
            fingerprint, prior = self.tasks._command(conn, context, command_id, 'control_delegate', args)
            if prior:
                receipt = prior
            else:
                delegate = conn.execute('SELECT d.* FROM manager_delegates d JOIN agent_sessions s '
                                        'ON s.session_id=d.session_id WHERE d.delegate_id=? AND d.parent_session_id=? '
                                        "AND s.kind='manager' AND s.status!='deleted'",
                                        (delegate_id, context.session_id)).fetchone()
                if delegate is None or delegate['state'] != 'active':
                    raise ManagementConflictError('Delegate is unavailable, terminal or not owned by this manager')
                if delegate['revision'] != expected_revision:
                    raise ManagementConflictError('Delegate revision changed; read it before control')
                roots = conn.execute('SELECT r.run_id,r.session_id,r.status FROM agent_runs r WHERE '
                                     "json_extract(r.record_payload,'$.parent_run_id') IS NULL AND (r.session_id=? OR EXISTS ("
                                     'SELECT 1 FROM managed_session_links l WHERE l.manager_session_id=? '
                                     'AND l.task_session_id=r.session_id)) ORDER BY r.created_at,r.run_id',
                                     (delegate['session_id'], delegate['session_id'])).fetchall()
                affected = [dict(row) for row in roots if row['status'] not in TERMINAL]
                stamp = datetime.now(UTC).isoformat()
                state = 'cancelled' if action == 'cancel' else 'active'
                conn.execute('UPDATE manager_delegates SET state=?,revision=revision+1,updated_at=? WHERE delegate_id=?',
                             (state, stamp, delegate_id))
                conn.execute("UPDATE manager_controllers SET status='paused',revision=revision+1,updated_at=? WHERE session_id=?",
                             (stamp, delegate['session_id']))
                receipt = {'command_id': command_id, 'action': 'control_delegate', 'control_action': action,
                           'status': 'accepted', 'delegate_id': delegate_id, 'session_id': delegate['session_id'],
                           'state': state, 'controller_status': 'paused', 'revision': expected_revision + 1,
                           'affected_roots': affected, 'manager_run_id': context.run_id}
                conn.execute('INSERT INTO management_commands VALUES(?,?,?,?,?,?)',
                             (context.session_id, command_id, fingerprint, 'control_delegate', _json(receipt), stamp))
        # The state fence is already durable. Replaying the command replays this
        # signaling too, including after a crash between commit and cancellation.
        for root in receipt['affected_roots']:
            if self.tasks.run_manager.get_run(root['run_id']) is None:
                raise ManagementConflictError('Controlled root is unavailable')
            reason = 'Delegate ' + action + ': ' + delegate_id
            self.tasks.run_manager.request_cancel(root['run_id'], reason=reason)
            self.tasks.run_manager.cancel_run(root['run_id'], reason=reason)
        return receipt

    @staticmethod
    def _summary(row):
        row = dict(row)
        return {key: row[key] for key in ('delegate_id', 'session_id', 'parent_session_id', 'state', 'revision',
                                         'created_at', 'updated_at')} | ({'controller_status': row['controller_status']}
                                                                    if 'controller_status' in row else {})

    def list_delegates(self, context, *, limit=30, offset=0):
        self.tasks.caller(context)
        self.tasks._page(limit, offset)
        with closing(self._conn()) as conn:
            rows = conn.execute('SELECT d.*,c.status AS controller_status FROM manager_delegates d '
                                'JOIN manager_controllers c ON c.session_id=d.session_id '
                                'WHERE d.parent_session_id=? OR d.session_id=? '
                                'ORDER BY d.created_at,d.delegate_id LIMIT ? OFFSET ?',
                                (context.session_id, context.session_id, limit + 1, offset)).fetchall()
        return {'delegates': [{**self._summary(row), 'objective_preview': row['objective'][:500]} for row in rows[:limit]],
                'next_offset': offset + limit if len(rows) > limit else None}

    def read_delegate(self, context, *, delegate_id, start_char=0):
        self.tasks.caller(context)
        if type(start_char) is not int or start_char < 0:
            raise ValueError('start_char must be non-negative')
        with closing(self._conn()) as conn:
            conn.execute('BEGIN')
            row = conn.execute('SELECT d.*,c.status AS controller_status FROM manager_delegates d '
                               'JOIN manager_controllers c ON c.session_id=d.session_id '
                               'WHERE d.delegate_id=? AND (d.parent_session_id=? OR d.session_id=?)',
                               (delegate_id, context.session_id, context.session_id)).fetchone()
            if row is None:
                raise ManagementConflictError('Delegate is not owned by this manager')
            latest_run = self.tasks._latest_run(conn, row['session_id'])
            reports = [dict(r) for r in conn.execute('SELECT * FROM manager_delegate_reports WHERE delegate_id=? '
                                                     'ORDER BY created_at,report_id', (delegate_id,))]
        # One continuation retains complete requirements, provenance and reports.
        content = _json({'objective': row['objective'],
                         'source_envelope': self._source_envelope(json.loads(row['kickoff_envelope'])),
                         'reports': [{**r, 'evidence': json.loads(r['evidence'])} for r in reports]})
        return {**self._summary(row), 'latest_run': latest_run, 'content': content[start_char:start_char + 4000],
                'next_char': start_char + 4000 if start_char + 4000 < len(content) else None}

    def _evidence(self, conn, session_id, evidence):
        evidence = [] if evidence is None else evidence
        if not isinstance(evidence, list) or len(evidence) > 20:
            raise ValueError('evidence must be a list of at most 20 references')
        for ref in evidence:
            if (not isinstance(ref, dict) or not ref or set(ref) - {'session_id', 'run_id', 'message_id'}
                    or any(not isinstance(v, str) or not v or len(v) > 256 for v in ref.values())):
                raise ValueError('Evidence requires bounded session_id, run_id and/or message_id references')
            sid, rid = ref.get('session_id'), ref.get('run_id')
            if rid:
                row = conn.execute('SELECT session_id FROM agent_runs WHERE run_id=?', (rid,)).fetchone()
                if row is None or (sid is not None and sid != row['session_id']):
                    raise ManagementConflictError('Evidence run does not belong to the referenced session')
                sid = row['session_id']
            if ref.get('message_id'):
                row = conn.execute('SELECT session_id,payload FROM agent_session_messages WHERE message_id=?',
                                   (ref['message_id'],)).fetchone()
                if row is None or (sid is not None and sid != row['session_id']):
                    raise ManagementConflictError('Evidence message does not belong to the referenced session')
                if rid and json.loads(row['payload']).get('run_id') != rid:
                    raise ManagementConflictError('Evidence message lacks matching run provenance')
                sid = row['session_id']
            row = conn.execute('SELECT 1 FROM managed_session_links l JOIN agent_sessions s '
                               "ON s.session_id=l.task_session_id WHERE l.manager_session_id=? AND l.task_session_id=? "
                               "AND s.kind='ordinary' AND s.status!='deleted'", (session_id, sid)).fetchone()
            if row is None:
                raise ManagementConflictError('Evidence must reference this delegate\'s ordinary task sessions')
        return evidence

    @staticmethod
    def _finished_tasks(conn, session_id):
        rows = conn.execute('SELECT r.status FROM agent_runs r JOIN managed_session_links l ON l.task_session_id=r.session_id '
                            'WHERE l.manager_session_id=?', (session_id,)).fetchall()
        if any(row['status'] not in TERMINAL for row in rows):
            raise ManagementConflictError('Complete or cancel unfinished ordinary tasks before reporting completion')

    @staticmethod
    def _require_task_evidence(conn, session_id, evidence):
        if not evidence and conn.execute(
            "SELECT 1 FROM managed_session_links l JOIN agent_sessions s ON s.session_id=l.task_session_id "
            "WHERE l.manager_session_id=? AND s.kind='ordinary' AND s.status!='deleted' LIMIT 1", (session_id,),
        ).fetchone():
            raise ManagementConflictError(
                'Reports from a delegate with ordinary tasks require nonempty evidence. '
                'Read the relevant owned task with management.read_task_result and pass its '
                'session_id, run_id and/or message_id in evidence; pending or failed task references '
                'may support blocked/needs_decision reports. Do not repeat execution or infer references from prose.'
            )

    def report_delegate(self, context, *, command_id, kind, summary, evidence=None):
        if kind not in ('progress', 'completed', 'needs_decision', 'blocked'):
            raise ValueError('Invalid delegate report kind')
        if not isinstance(summary, str) or not summary.strip() or len(summary) > 4000:
            raise ValueError('summary must contain 1..4000 characters')
        with closing(self._conn()) as conn, conn:
            conn.execute('BEGIN IMMEDIATE')
            run = self.tasks.caller(context, mutate=True, conn=conn)
            delegate = self.check_session(conn, context.session_id)
            if delegate is None:
                raise ManagementConflictError('Only a delegate can report')
            execution_id = run.metadata.get('manager_execution_id')
            execution = conn.execute('SELECT run_id,session_id,status FROM manager_executions WHERE execution_id=?',
                                     (execution_id,)).fetchone() if execution_id else None
            if (execution is None or execution['run_id'] != context.run_id or execution['session_id'] != context.session_id
                    or execution['status'] != 'bound'):
                raise ManagementConflictError('Reports require the current autonomous execution')
            args = {'kind': kind, 'summary': summary, 'evidence': evidence or []}
            fingerprint, prior = self.tasks._command(conn, context, command_id, 'report_delegate', args)
            if prior:
                return prior
            if conn.execute("SELECT 1 FROM manager_delegate_report_proposals WHERE execution_id=? AND kind='completed'",
                            (execution_id,)).fetchone():
                raise ManagementConflictError('A completion report is already proposed for this execution')
            evidence = self._evidence(conn, context.session_id, evidence)
            if kind == 'completed':
                self._finished_tasks(conn, context.session_id)
            self._require_task_evidence(conn, context.session_id, evidence)
            conn.execute('INSERT INTO manager_delegate_report_proposals VALUES(?,?,?,?,?,?,?)',
                         (execution_id, command_id, delegate['delegate_id'], context.run_id, kind, summary, _json(evidence)))
            receipt = {'command_id': command_id, 'action': 'report_delegate', 'status': 'proposed',
                       'delegate_id': delegate['delegate_id'], 'execution_id': execution_id, 'kind': kind}
            conn.execute('INSERT INTO management_commands VALUES(?,?,?,?,?,?)',
                         (context.session_id, command_id, fingerprint, 'report_delegate', _json(receipt), datetime.now(UTC).isoformat()))
            return receipt

    def apply_reports(self, conn, execution_id, session_id):
        rows = conn.execute('SELECT * FROM manager_delegate_report_proposals WHERE execution_id=? ORDER BY rowid',
                            (execution_id,)).fetchall()
        for row in rows:
            if conn.execute('SELECT 1 FROM manager_delegate_reports WHERE delegate_id=? AND command_id=?',
                            (row['delegate_id'], row['command_id'])).fetchone():
                continue
            delegate = self.check_session(conn, session_id)
            if delegate is None or delegate['delegate_id'] != row['delegate_id']:
                raise ManagementConflictError('Report proposal owner changed')
            evidence = self._evidence(conn, session_id, json.loads(row['evidence']))
            if row['kind'] == 'completed':
                self._finished_tasks(conn, session_id)
            self._require_task_evidence(conn, session_id, evidence)
            report_id, stamp = 'delegate_report_' + uuid4().hex, datetime.now(UTC).isoformat()
            conn.execute('INSERT INTO manager_delegate_reports VALUES(?,?,?,?,?,?,?,?)',
                         (report_id, row['delegate_id'], row['run_id'], row['command_id'], row['kind'], row['summary'], row['evidence'], stamp))
            self.goals._event(conn, delegate['parent_session_id'], 'delegate_report:' + report_id, 'timer',
                              {'delegate_report': report_id, 'delegate_id': row['delegate_id'], 'session_id': session_id,
                               'report_kind': row['kind'], 'summary': row['summary'][:700]}, stamp)
            if row['kind'] == 'completed':
                conn.execute("UPDATE manager_delegates SET state='completed',revision=revision+1,updated_at=? WHERE delegate_id=?",
                             (stamp, row['delegate_id']))

    def capture_events(self):
        """Surface execution failure or a missing handoff without interpreting results."""
        captured = 0
        with closing(self._conn()) as conn, conn:
            conn.execute('BEGIN IMMEDIATE')
            rows = conn.execute("SELECT d.delegate_id,d.parent_session_id,d.session_id,d.revision,r.record_payload "
                                "FROM manager_delegates d JOIN agent_runs r ON r.run_id=("
                                "SELECT a.run_id FROM agent_runs a WHERE a.session_id=d.session_id "
                                "AND json_extract(a.record_payload,'$.parent_run_id') IS NULL "
                                "ORDER BY a.created_at DESC,a.run_id DESC LIMIT 1) WHERE d.state='active'").fetchall()
            for row in rows:
                record = json.loads(row['record_payload'])
                status = record['status']
                payload = {'delegate_execution_issue': row['delegate_id'], 'delegate_id': row['delegate_id'],
                           'session_id': row['session_id'], 'run_id': record['run_id'], 'status': status, 'delegate_revision': row['revision']}
                if status in ('waiting_user', 'waiting_confirmation'):
                    waiting = self.goals._waiting_payload(conn, record)
                    if waiting is None:
                        continue
                    payload.update(waiting)
                    view = self.tasks._run_view(record)
                    if view.get('pending_user_question'):
                        question = view['pending_user_question']
                        payload['pending_user_question'] = {'question_id': question['question_id'],
                                                            'question': question['question'][:350]}
                    if view.get('user_control'):
                        payload['user_control'] = view['user_control']
                elif status == 'completed':
                    # A finished dispatching turn is waiting for the next result
                    # turn, even if all its tasks finished between two scans.
                    if conn.execute("SELECT 1 FROM manager_inbox WHERE session_id=? AND consumed_execution_id IS NULL "
                                    "AND (kind='task_terminal' OR json_extract(payload,'$.task_waiting')=1) LIMIT 1",
                                    (row['session_id'],)).fetchone():
                        continue
                    if conn.execute('SELECT 1 FROM manager_delegate_reports WHERE run_id=?', (record['run_id'],)).fetchone():
                        continue
                    try:
                        self._finished_tasks(conn, row['session_id'])
                    except ManagementConflictError:
                        continue
                    payload['status'] = 'missing_report'
                elif status not in ('failed', 'cancelled', 'timed_out'):
                    continue
                dedup = 'delegate_execution_issue:' + record['run_id'] + ':' + payload['status']
                if payload.get('task_waiting'):
                    dedup += ':' + str(payload.get('question_id') or payload.get('review_id') or '')
                captured += self.goals._event(conn, row['parent_session_id'], dedup, 'timer', payload,
                                               datetime.now(UTC).isoformat())
        return captured

    def validate_event(self, conn, event, *, consuming_run_id=None):
        payload = event['payload']
        payload = json.loads(payload) if isinstance(payload, str) else payload
        if payload.get('delegate_execution_issue'):
            row = conn.execute('SELECT d.delegate_id,d.session_id,d.parent_session_id,d.revision,d.state,r.record_payload '
                               'FROM manager_delegates d JOIN agent_runs r ON r.session_id=d.session_id '
                               'WHERE d.delegate_id=? AND r.run_id=?',
                               (payload['delegate_execution_issue'], payload.get('run_id'))).fetchone()
            if (row is None or row['parent_session_id'] != event['session_id']
                    or row['session_id'] != payload.get('session_id') or row['delegate_id'] != payload.get('delegate_id')):
                return False
            record = json.loads(row['record_payload'])
            latest = self.tasks._latest_run(conn, row['session_id'])
            if (row['state'] != 'active' or row['revision'] != payload.get('delegate_revision')
                    or not latest or latest['run_id'] != record['run_id']):
                # A bound parent may consume the issue that its own accepted
                # lifecycle command resolved. Pending event discovery never
                # receives this exception, nor does a later parent execution.
                return bool(consuming_run_id and conn.execute(
                    "SELECT 1 FROM management_commands WHERE manager_session_id=? "
                    "AND action IN ('instruct_delegate','control_delegate') "
                    "AND json_extract(receipt,'$.manager_run_id')=? "
                    "AND json_extract(receipt,'$.delegate_id')=? "
                    "AND json_extract(receipt,'$.revision')=? LIMIT 1",
                    (event['session_id'], consuming_run_id, row['delegate_id'], row['revision'])).fetchone())
            status = payload.get('status')
            if payload.get('task_waiting'):
                current = self.goals._waiting_payload(conn, record)
                return bool(current and all(current.get(key) == payload.get(key)
                                             for key in ('status', 'question_id', 'review_id')))
            if status == 'missing_report':
                if record['status'] != 'completed' or conn.execute('SELECT 1 FROM manager_delegate_reports WHERE run_id=?',
                                                                  (record['run_id'],)).fetchone():
                    return False
                try:
                    self._finished_tasks(conn, row['session_id'])
                except ManagementConflictError:
                    return False
                return True
            return status in ('failed', 'cancelled', 'timed_out') and record['status'] == status
        if payload.get('delegate_instruction'):
            item = self.get_for_session(conn, event['session_id'])
            if (item is None or item['delegate_id'] != payload['delegate_instruction']
                    or item['revision'] != payload.get('revision')):
                return False
            try:
                self.check_session(conn, event['session_id'])
            except ManagementConflictError:
                return False
        if payload.get('delegate_kickoff'):
            item = self.get_for_session(conn, event['session_id'])
            if item is None or item['delegate_id'] != payload['delegate_kickoff']:
                return False
            try:
                self.check_session(conn, event['session_id'])
            except ManagementConflictError:
                return False
        if payload.get('delegate_report'):
            row = conn.execute('SELECT d.parent_session_id,d.session_id,r.delegate_id,r.kind FROM manager_delegate_reports r '
                               'JOIN manager_delegates d ON d.delegate_id=r.delegate_id WHERE r.report_id=?',
                               (payload['delegate_report'],)).fetchone()
            if row is None or any((row['parent_session_id'] != event['session_id'],
                                   row['delegate_id'] != payload.get('delegate_id'), row['session_id'] != payload.get('session_id'),
                                   row['kind'] != payload.get('report_kind'))):
                return False
        return True
