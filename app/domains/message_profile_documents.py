"""Private local living dossiers for isolated replay; never enables analysis.

Production intelligence remains in SQLite. This explicitly invoked store is a
local export/replay artifact, not a replacement for its policy-fenced database.
Generated revisions are immutable pairs, published with an atomic manifest;
human notes live outside generated files and are never rewritten.
"""
from __future__ import annotations

import hashlib
import html
import json
import os
import re
import shutil
import tempfile
from copy import deepcopy
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

from app.domains.message_participant_profiles import (
    _DISALLOWED,
    ParticipantClaim,
    ParticipantProfileIndex,
    _Participant,
)


def _key(value: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError("profile_identity_required")
    return hashlib.sha256(value.encode()).hexdigest()[:24]


def _atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(prefix=".profile-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            output.write(text)
            output.flush()
            os.fsync(output.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def _json(value: dict) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"


def _md(value: object) -> str:
    return re.sub(r"([\\`*_{}\[\]()#+.!|>~-])", r"\\\1", html.escape(str(value))).replace("\n", " ")


def _time(value: int | None) -> str:
    if type(value) is not int:
        return "未知"
    return datetime.fromtimestamp(value, timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S") + "（上海时间）"


CATEGORIES = {"communication_style": "沟通方式", "recurring_topic": "反复出现的主题",
    "experience": "本人自述经历", "role": "本人自述角色", "context_event": "当前处境与事件",
    "need": "需求", "preference": "偏好", "characteristic": "旧版特点观察"}
BASES = {"explicit": "本人自述（未独立核实）", "observed": "有重复证据的行为观察", "uncertain": "暂定观察（证据有限）"}
STATUSES = {"candidate": "候选", "stale": "已过期", "archived": "历史存档",
            "contested": "存在冲突，待核对", "superseded": "已被更正替代"}
UNVERIFIED_REASONS = frozenset({"not_direct_self_statement", "unverified_paraphrase",
    "observed_requires_independent_windows", "observed_requires_temporary_wording",
    "self_report_requires_explicit_basis", "behavior_requires_observation"})


class MessageProfileDocumentStore:
    """Single-writer store, intended for the isolated sequential replay runner.

    Inputs must have stable, conversation-scoped sender and message identities.
    Capture epoch is monotonically increasing; older epochs cannot republish.
    New epochs drop derived evidence, retaining suppression and human notes.
    """

    def __init__(self, root: str | Path, *, history_limit: int = 120, revision_limit: int = 20,
                 participant_limit: int = 1000, retain_unverified_notes: bool = False):
        self.root = Path(root)
        if any(type(value) is not int or value < 1 for value in
               (history_limit, revision_limit, participant_limit)):
            raise ValueError("invalid_dossier_retention")
        self.history_limit = history_limit
        self.revision_limit = revision_limit
        self.participant_limit = participant_limit
        if type(retain_unverified_notes) is not bool:
            raise ValueError("invalid_unverified_notes_option")
        self.retain_unverified_notes = retain_unverified_notes

    def _path(self, conversation_id: str) -> Path:
        return self.root / _key(conversation_id)

    def _load(self, conversation_id: str, capture_epoch: int) -> dict:
        if type(capture_epoch) is not int or capture_epoch < 0:
            raise ValueError("invalid_capture_epoch")
        path = self._path(conversation_id) / "state.json"
        if not path.exists():
            return {"conversation_id": conversation_id, "capture_epoch": capture_epoch,
                    "revision": 0, "participants": {}}
        state = json.loads(path.read_text(encoding="utf-8"))
        if state["conversation_id"] != conversation_id:
            raise ValueError("profile_identity_collision")
        if capture_epoch < state["capture_epoch"]:
            raise PermissionError("stale_profile_capture_epoch")
        if capture_epoch > state["capture_epoch"]:
            state["capture_epoch"] = capture_epoch
            state["participants"] = {sender: {
                "sender": sender, "claims": [], "machine_notes": [], "evidence": {},
                "revision": person["revision"] + 1,
                "suppressed": person.get("suppressed", False),
                "hidden": person.get("hidden", False),
            }
                for sender, person in state["participants"].items()}
        self._privacy_filter(state)
        return state

    def _privacy_filter(self, state: dict) -> None:
        """Hide forbidden historical inferences, keeping human notes untouched.

        Immutable private export history and original message snapshots are not
        purged. Filtered source-of-truth export state is persisted on next ingest.
        """
        removed = 0
        for person in state["participants"].values():
            for field in ("claims", "machine_notes"):
                existing = person.get(field, [])
                allowed = [value for value in existing if not _DISALLOWED.search(
                    value.get("text", "") + " " + value.get("quote", "") + " " +
                    " ".join(value.get("evidence_quotes", {}).values()))]
                removed += len(existing) - len(allowed)
                if len(allowed) != len(existing):
                    person[field] = allowed
                    person["revision"] += 1
            references = {alias for value in [*person.get("claims", []), *person.get("machine_notes", [])]
                          for alias in value["source_ids"]}
            person["evidence"] = {alias: value for alias, value in person["evidence"].items()
                                  if alias in references}
        if removed:
            state["privacy_filtered_count"] = state.get("privacy_filtered_count", 0) + removed

    def ingest(self, conversation_id: str, claims: list[dict], messages: list[dict], now: int,
               *, capture_epoch: int = 0) -> dict:
        state_path = self._path(conversation_id) / "state.json"
        previous_state = state_path.read_text(encoding="utf-8") if state_path.exists() else None
        state = self._load(conversation_id, capture_epoch)
        sources = {row["id"]: row for row in messages}
        if len(sources) != len(messages):
            raise ValueError("ambiguous_source_alias")
        accepted, retained_notes, rejected = 0, 0, []
        # Sparse authors receive a dossier even when there is no valid inference.
        for row in messages:
            sender = row.get("sender")
            if isinstance(sender, str) and sender:
                if sender not in state["participants"] and len(state["participants"]) >= self.participant_limit:
                    continue
                state["participants"].setdefault(sender, {"sender": sender, "claims": [],
                    "evidence": {}, "revision": 0, "suppressed": False})
        for claim in claims:
            sender = claim.get("sender")
            person = state["participants"].get(sender)
            if person is None and len(state["participants"]) >= self.participant_limit:
                rejected.append({"sender": sender, "reason": "participant_capacity"})
                continue
            if person and (person.get("hidden") or person["suppressed"]):
                continue
            if person and any(all(previous.get(key, {} if key == "evidence_quotes" else None) ==
                                  claim.get(key, {} if key == "evidence_quotes" else None)
                                  for key in ("sender", "kind", "text", "source_ids", "quote",
                                              "basis", "valid_until", "evidence_quotes", "facet"))
                              for previous in person["claims"]):
                continue
            index = ParticipantProfileIndex(conversation_id)
            if person:
                # Validator uses bounded active memory; dossier history remains durable.
                index._people[sender] = _Participant(claims=[value for value in person["claims"]
                    if value.get("status") != "archived"][-12:],
                    revision=person["revision"], suppressed=person["suppressed"])
            if not index.add_claim(claim, messages, now):
                rejected.append({"sender": sender, "reason": index.last_rejection_reason})
                if self.retain_unverified_notes and index.last_rejection_reason in UNVERIFIED_REASONS:
                    person = state["participants"][sender]
                    if not person["suppressed"]:
                        retained_notes += self._retain_note(person, claim, sources, now, index.last_rejection_reason)
                continue
            validated = index._people[sender]
            person = state["participants"].setdefault(sender, {"sender": sender, "claims": [],
                "evidence": {}, "revision": 0, "suppressed": False})
            if validated.revision == person["revision"]:
                continue
            # The validator sorts live observations by observation time; replay
            # of an earlier chunk must not mistake a newer claim for this one.
            observations = [value for value in validated.claims if all(value.get(key) == raw
                            for key, raw in claim.items())]
            if not observations:
                # An older observation can fall outside the bounded live window.
                archive_index = ParticipantProfileIndex(conversation_id)
                archive_index.add_claim({**claim, "supersedes": []}, messages, now)
                observation = archive_index._people[sender].claims[-1]
                observation["supersedes"] = claim.get("supersedes", [])
                observation["status"] = "archived"
            else:
                observation = observations[0]
            for alias in observation["source_ids"]:
                row = sources[alias]
                evidence = {key: row.get(key) for key in
                    ("id", "sender", "seq", "sent_at", "received_at", "text", "kind", "reply", "parts")}
                old = person["evidence"].get(alias)
                if old is not None and old != evidence:
                    raise ValueError("message_alias_collision")
                person["evidence"][alias] = evidence
            updated = {value["claim_id"]: value for value in validated.claims}
            person["claims"] = [updated.get(value["claim_id"], value) for value in person["claims"]]
            for value in person["claims"]:
                if value["claim_id"] not in updated:
                    value["status"] = "archived"
            person["claims"].append(observation)
            person["claims"].sort(key=lambda value: (value["created_at"], value["claim_id"]))
            trimmed = max(0, len(person["claims"]) - self.history_limit)
            person["history_pruned_count"] = person.get("history_pruned_count", 0) + trimmed
            person["claims"] = person["claims"][-self.history_limit:]
            retained_sources = {alias for value in [*person["claims"], *person.get("machine_notes", [])]
                                for alias in value["source_ids"]}
            person["evidence"] = {alias: value for alias, value in person["evidence"].items()
                                  if alias in retained_sources}
            person["revision"] += 1
            accepted += 1
        if _json(state) != previous_state:
            state["revision"] += 1
            state["updated_at"] = max(state.get("updated_at", 0), now)
            _atomic(state_path, _json(state))
        return {"accepted": accepted, "retained_notes": retained_notes, "rejected": rejected, "revision": state["revision"]}

    def _retain_note(self, person: dict, claim: dict, sources: dict, now: int, reason: str) -> int:
        """Called only after strict author's exact evidence and safety guards passed.

        Keep rejected semantic proposals outside accepted claims. No basis is
        silently downgraded: the original proposed basis remains visible.
        """
        candidate = ParticipantClaim.model_validate(claim).model_dump()
        if candidate["valid_until"] is not None and candidate["valid_until"] < 0:
            return 0
        note_id = hashlib.sha256(_json(candidate).encode()).hexdigest()
        notes = person.setdefault("machine_notes", [])
        if any(value["note_id"] == note_id for value in notes):
            return 0
        for alias in candidate["source_ids"]:
            row = sources[alias]
            evidence = {key: row.get(key) for key in
                        ("id", "sender", "seq", "sent_at", "received_at", "text", "kind", "reply", "parts")}
            if alias in person["evidence"] and person["evidence"][alias] != evidence:
                raise ValueError("message_alias_collision")
            person["evidence"][alias] = evidence
        notes.append({"note_id": note_id, "proposed_kind": candidate["kind"],
            "proposed_basis": candidate["basis"], "text": candidate["text"],
            "quote": candidate["quote"], "evidence_quotes": candidate["evidence_quotes"],
            "source_ids": candidate["source_ids"], "facet": candidate["facet"],
            "created_at": now, "proposed_valid_until": candidate["valid_until"],
            "expires_at": candidate["valid_until"] if candidate["valid_until"] is not None
                else now + (7 if candidate["kind"] == "need" else 30) * 86400,
            "status": "needs_review", "rejection_reason": reason,
            "validation": "exact_authored_evidence_only_semantic_proposal_unverified"})
        notes.sort(key=lambda value: (value["created_at"], value["note_id"]))
        for value in notes[:-12]:
            value["status"] = "archived"
        removed = max(0, len(notes) - self.history_limit)
        person["machine_notes_pruned_count"] = person.get("machine_notes_pruned_count", 0) + removed
        person["machine_notes"] = notes[-self.history_limit:]
        retained = {alias for value in [*person["claims"], *person["machine_notes"]]
                    for alias in value["source_ids"]}
        person["evidence"] = {alias: value for alias, value in person["evidence"].items() if alias in retained}
        person["revision"] += 1
        return 1

    def apply_controls(self, conversation_id: str, *, hidden_senders: set[str],
                       suppressed_senders: set[str], capture_epoch: int = 0) -> None:
        """Mirror native participant visibility controls before document export."""
        state_path = self._path(conversation_id) / "state.json"
        previous_state = state_path.read_text(encoding="utf-8") if state_path.exists() else None
        state = self._load(conversation_id, capture_epoch)
        for sender in suppressed_senders | hidden_senders:
            state["participants"].setdefault(sender, {"sender": sender, "claims": [],
                "machine_notes": [], "evidence": {}, "revision": 0, "suppressed": False})
        changed = False
        for sender, person in state["participants"].items():
            suppressed = sender in suppressed_senders
            hidden = sender in hidden_senders and not suppressed
            if suppressed and not person.get("suppressed"):
                person.update(suppressed=True, claims=[], machine_notes=[], evidence={})
                person["revision"] += 1
                changed = True
            if person.get("hidden", False) != hidden:
                person["hidden"] = hidden
                person["revision"] += 1
                changed = True
        if changed or _json(state) != previous_state:
            state["revision"] += 1
            state["updated_at"] = int(datetime.now(UTC).timestamp())
            _atomic(state_path, _json(state))

    def snapshot(self, conversation_id: str, *, capture_epoch: int = 0, now: int | None = None) -> dict:
        state = self._load(conversation_id, capture_epoch)
        participants = []
        for sender, person in sorted(state["participants"].items()):
            if person["suppressed"] or person.get("hidden", False):
                continue
            evidence = list(person["evidence"].values())
            times = [row["sent_at"] if type(row.get("sent_at")) is int else row["received_at"]
                     for row in evidence]
            sequences = [row["seq"] for row in evidence if type(row.get("seq")) is int]
            participants.append({**deepcopy(person), "identity": [conversation_id, sender],
                "status": "observations" if person["claims"] else
                    "needs_review" if person.get("machine_notes") else "insufficient_information",
                "source_range": {"first_time": min(times, default=None),
                    "last_time": max(times, default=None), "first_seq": min(sequences, default=None),
                    "last_seq": max(sequences, default=None)}})
            for claim in participants[-1]["claims"]:
                if claim.get("status") == "candidate" and (state.get("updated_at", 0) if now is None else now) >= claim["expires_at"]:
                    claim["status"] = "stale"
            cutoff = state.get("updated_at", 0) if now is None else now
            for note in participants[-1].get("machine_notes", []):
                expires = note.get("expires_at", note["created_at"] + 30 * 86400)
                if note["status"] == "needs_review" and cutoff >= expires:
                    note["status"] = "stale"
        return {**state, "participants": participants, "untrusted_data": True,
                "validation": "source_linked_revisable_observations_not_fact_certification"}

    def suppress(self, conversation_id: str, sender: str, *, capture_epoch: int = 0) -> None:
        state = self._load(conversation_id, capture_epoch)
        person = state["participants"].setdefault(sender, {"sender": sender, "revision": 0})
        person.update(suppressed=True, claims=[], machine_notes=[], evidence={}, revision=person["revision"] + 1)
        state["revision"] += 1
        _atomic(self._path(conversation_id) / "state.json", _json(state))
        # A suppressed person's current entry must not point to an older dossier.
        person_dir = self._path(conversation_id) / "people" / _key(sender)
        _atomic(person_dir / "current.json", _json({"sender": sender, "suppressed": True,
                "capture_epoch": capture_epoch, "revision": person["revision"]}))
        self.export(conversation_id, capture_epoch=capture_epoch)

    def export(self, conversation_id: str, *, capture_epoch: int = 0,
               display_names: dict[str, str] | None = None, now: int | None = None) -> dict:
        snapshot = self.snapshot(conversation_id, capture_epoch=capture_epoch, now=now)
        group = self._path(conversation_id)
        render_key = hashlib.sha256(_json({"snapshot": snapshot, "display_names": display_names or {}}).encode()).hexdigest()[:12]
        version = f"epoch-{capture_epoch}-revision-{snapshot['revision']}-{render_key}"
        revision = group / "revisions" / version
        markdown = [f"# 会话人物档案：{_md(conversation_id)}", "",
            "本文由程序维护；人工补充请写入独立 notes.md，生成更新不会覆盖人工笔记。", "",
            "以下均为关联原文、可修订的观察。本人自述并不代表事实已经核实。", "",
            f"档案更新时间：{_time(snapshot.get('updated_at'))}；版本：{snapshot['revision']}。", ""]
        people_paths = {}
        for person in snapshot["participants"]:
            sender = person["sender"]
            person["display_name"] = (display_names or {}).get(sender, sender)
            notes = group / "people" / _key(sender) / "notes.md"
            notes.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            try:
                with notes.open("x", encoding="utf-8") as output:
                    output.write("# 人工笔记\n\n")
            except FileExistsError:
                pass
            start = len(markdown)
            markdown.extend([f"## {_md(person['display_name'])}（本地真实 ID：{_md(sender)}）", "",
                "信息状态：" + ("有可追溯观察" if person["claims"] else
                    "仅有待核实机器摘记" if person.get("machine_notes") else "信息不足，不生成推断"), "",
                f"证据范围：{_time(person['source_range']['first_time'])} 至 {_time(person['source_range']['last_time'])}。", ""])
            for category, label in CATEGORIES.items():
                category_claims = [value for value in person["claims"] if value["kind"] == category]
                if not category_claims:
                    continue
                markdown.extend([f"### {label}", ""])
                for claim in category_claims:
                    status = claim.get("status", "candidate")
                    markdown.extend([f"- {_md(claim['text'])}",
                        f"  - 依据：{BASES[claim['basis']]}；状态：{STATUSES.get(status, status)}（{status}）。",
                        f"  - 记录时间：{_time(claim['created_at'])}；有效至：{_time(claim['expires_at'])}。"])
                    for alias in claim["source_ids"]:
                        source = person["evidence"][alias]
                        quote = claim.get("evidence_quotes", {}).get(alias, claim["quote"])
                        source_time = source.get("sent_at") if type(source.get("sent_at")) is int else source.get("received_at")
                        markdown.append(f"  - 原文 {_md(alias)} · {_time(source_time)}：{_md(quote)}")
                markdown.append("")
            if person.get("machine_notes"):
                markdown.extend(["### 待核实机器摘记", "",
                    "以下内容是机器提出但未通过语义强度校验的摘记，绝非已接受画像、本人确认或已核实事实。原文归属与逐字引用已校验；提议含义仍需人工核对。", ""])
                for note in person["machine_notes"]:
                    markdown.extend([f"- {_md(note['text'])}",
                        f"  - 原提议类别：{CATEGORIES[note['proposed_kind']]}；原提议依据：{_md(note['proposed_basis'])}；状态："
                        + {"archived": "历史机器摘记（archived）", "stale": "已过期机器摘记（stale）",
                           "needs_review": "待核实（needs_review）"}[note["status"]] + "。",
                        (f"  - 未通过原因：{_md(note['rejection_reason'])}；记录时间：{_time(note['created_at'])}；"
                         f"有效至：{_time(note.get('expires_at'))}。")])
                    for alias in note["source_ids"]:
                        source = person["evidence"][alias]
                        stamp = source.get("sent_at") if type(source.get("sent_at")) is int else source.get("received_at")
                        quote = note["evidence_quotes"].get(alias, note["quote"])
                        markdown.append(f"  - 原文 {_md(alias)} · {_time(stamp)}：{_md(quote)}")
                markdown.append("")
            markdown.append("")
            person_revision = notes.parent / "revisions" / version
            _atomic(person_revision / "dossier.json", _json(person))
            _atomic(person_revision / "dossier.md", "本文由程序维护；人工补充请写入独立 notes.md。\n\n" + "\n".join(markdown[start:]) + "\n")
            people_paths[sender] = {"json": str(person_revision / "dossier.json"),
                                    "markdown": str(person_revision / "dossier.md"), "notes": str(notes)}
            _atomic(notes.parent / "current.json", _json(people_paths[sender]))
            self._prune_versions(notes.parent / "revisions", person_revision)
        for sender, person in self._load(conversation_id, capture_epoch)["participants"].items():
            if sender in people_paths or not (person.get("hidden") or person.get("suppressed")):
                continue
            current = group / "people" / _key(sender) / "current.json"
            _atomic(current, _json({"sender": sender, "suppressed": bool(person.get("suppressed")),
                "hidden": bool(person.get("hidden")), "capture_epoch": capture_epoch,
                "revision": person["revision"]}))
        _atomic(revision / "dossier.json", _json(snapshot))
        _atomic(revision / "dossier.md", "\n".join(markdown) + "\n")
        paths = {"json": str(revision / "dossier.json"), "markdown": str(revision / "dossier.md"),
                 "participants": people_paths}
        _atomic(group / "current.json", _json({"revision": snapshot["revision"],
            "capture_epoch": capture_epoch, **paths}))
        self._prune_versions(group / "revisions", revision)
        return paths

    def _prune_versions(self, root: Path, current: Path) -> None:
        versions = sorted((path for path in root.iterdir() if path.is_dir() and not path.is_symlink()
                           and re.fullmatch(r"epoch-\d+-revision-\d+-[a-f0-9]{12}", path.name)),
                          key=lambda path: path.stat().st_mtime_ns, reverse=True)
        for path in versions[self.revision_limit:]:
            if path != current and {child.name for child in path.iterdir()} <= {"dossier.md", "dossier.json"}:
                shutil.rmtree(path)
