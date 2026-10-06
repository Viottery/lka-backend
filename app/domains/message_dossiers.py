"""Read local living dossiers through authoritative message policy and controls."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from app.domains.message_profile_documents import MessageProfileDocumentStore


class MessageDossierMixin:
    def _dossier_people(self, conn, policy, sender_id=None) -> list[dict[str, Any]]:
        store = MessageProfileDocumentStore(Path(self.db_path).parent / "message_profiles")
        directory = store._path(policy["conversation_key"])
        state_path = directory / "state.json"
        if not state_path.exists():
            return []
        try:
            if store.root.is_symlink() or directory.is_symlink() or state_path.is_symlink() or state_path.stat().st_size > 8 * 1024 * 1024:
                raise PermissionError("dossier_unavailable")
            snapshot = store.snapshot(policy["conversation_key"], capture_epoch=policy["capture_epoch"],
                                      now=self._intelligence_now())
        except (OSError, ValueError, KeyError, TypeError):
            raise PermissionError("dossier_unavailable") from None
        index, _, control_revision = self._intelligence_load(conn, policy)
        result = []
        for person in snapshot["participants"]:
            sender = person["sender"]
            if sender_id is not None and sender != sender_id:
                continue
            control = index._people.get(sender)
            if control is not None and (control.hidden or control.suppressed):
                continue
            evidence = person.get("evidence", {})
            ids = sorted(evidence)
            if len(ids) > 1000:
                continue
            rows = conn.execute("SELECT * FROM message_history_messages WHERE conversation_key=? "
                                "AND capture_epoch=? AND sender_id=? AND internal_message_id IN (" +
                                (",".join("?" for _ in ids) or "NULL") + ")",
                                [policy["conversation_key"], policy["capture_epoch"], sender, *ids]).fetchall()
            canonical = {row["internal_message_id"]: row for row in rows}
            filtered = {}
            # A stale file is not an authority. Require every quoted source to
            # still exist, belong to this author and contain the exact quote.
            for field in ("claims", "machine_notes"):
                filtered[field] = [value for value in person.get(field, []) if value.get("source_ids") and
                    all(source in canonical and
                        isinstance(value.get("evidence_quotes", {}).get(source, value.get("quote")), str) and
                        bool(value.get("evidence_quotes", {}).get(source, value.get("quote"))) and
                        value.get("evidence_quotes", {}).get(source, value.get("quote")) in canonical[source]["text"]
                        for source in value["source_ids"])]
            if control is not None and control.correction is not None:
                filtered = {"claims": [], "machine_notes": []}
            references = {source for values in filtered.values() for value in values for source in value["source_ids"]}
            if not references and (control is None or control.correction is None):
                continue
            sources = [self._resolved_message(conn, canonical[source]) for source in references]
            sources.sort(key=lambda row: row["seq"])
            result.append({"conversation_key": policy["conversation_key"], "sender_id": sender,
                "sender_name": sources[-1].get("sender_name") if sources else sender,
                "revision": person.get("revision", 0), "document_revision": snapshot["revision"],
                "control_revision": control_revision, "updated_at": snapshot.get("updated_at"),
                "capture_epoch": policy["capture_epoch"], **filtered,
                "human_correction": control.correction if control is not None else None,
                "sources": sources,
                "status": "human_corrected" if control is not None and control.correction is not None else
                          "observations" if filtered["claims"] else "needs_review",
                "validation": "source_linked_revisable_observations_not_fact_certification",
                "untrusted_data": True})
        return result

    def list_dossiers(self, conversation_key=None, *, limit=30, offset=0,
                      allowed_sources=None, allowed_accounts=None):
        size, start = self._page(limit, offset)
        with self._connection() as conn:
            policies = self._read_scope(conn, conversation_key, allowed_sources, allowed_accounts)
            people = []
            for policy in policies:
                for person in self._dossier_people(conn, policy):
                    people.append({key: value for key, value in person.items()
                                   if key not in {"claims", "machine_notes", "sources"}} | {
                        "claim_count": len(person["claims"]), "machine_note_count": len(person["machine_notes"]),
                        "source_count": len(person["sources"])})
            people.sort(key=lambda item: (item["conversation_key"], item["sender_id"]))
            more = len(people) > start + size
            return {"dossiers": people[start:start + size], "has_more": more,
                    "next_offset": start + size if more else None, "untrusted_data": True,
                    "coverage": {"kind": "local_dossier_exports", "page_only": True,
                                 "complete_platform_history": False}}

    def get_dossier(self, key, sender_id, *, limit=30, offset=0, allowed_sources=None, allowed_accounts=None):
        if not isinstance(sender_id, str) or not sender_id or len(sender_id) > 512:
            raise ValueError("invalid_dossier_identity")
        size, start = self._page(limit, offset)
        with self._connection() as conn:
            policies = self._read_scope(conn, key, allowed_sources, allowed_accounts)
            for policy in policies:
                for person in self._dossier_people(conn, policy, sender_id):
                    if person["sender_id"] == sender_id:
                        entries = [("claims", value) for value in person["claims"]] + [
                            ("machine_notes", value) for value in person["machine_notes"]]
                        selected = entries[start:start + size]
                        more = len(entries) > start + size
                        return {name: value for name, value in person.items()
                                if name not in {"sources", "claims", "machine_notes"}} | {
                            "claims": [value for field, value in selected if field == "claims"],
                            "machine_notes": [value for field, value in selected if field == "machine_notes"],
                            "observation_count": len(entries), "has_more": more,
                            "next_offset": start + size if more else None,
                            "source_count": len(person["sources"]),
                            "coverage": {"kind": "local_dossier_export", "capture_epoch": policy["capture_epoch"],
                                         "complete_platform_history": False}}
        raise PermissionError("dossier_unavailable")

    def dossier_sources(self, key, sender_id, *, limit=30, offset=0,
                        allowed_sources=None, allowed_accounts=None):
        size, start = self._page(limit, offset)
        size = min(size, 50)
        with self._connection() as conn:
            policies = self._read_scope(conn, key, allowed_sources, allowed_accounts)
            for policy in policies:
                for person in self._dossier_people(conn, policy, sender_id):
                    if person["sender_id"] == sender_id:
                        sources = person["sources"]
                        more = len(sources) > start + size
                        return {"sources": sources[start:start + size], "has_more": more,
                                "next_offset": start + size if more else None,
                                "capture_epoch": policy["capture_epoch"], "untrusted_data": True}
        raise PermissionError("dossier_unavailable")
