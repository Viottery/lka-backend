"""Fence message analysis when the effective processing configuration changes."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from app.domains.message_history import ANALYSIS_JOB_KIND, MessageHistoryService, _now


def _fingerprint(value: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=True,
                                    separators=(",", ":")).encode()).hexdigest()


def bind_reading_configuration(service: MessageHistoryService, *, provider: dict[str, Any],
                               processing: dict[str, Any]) -> None:
    """Bind the active configuration at startup, never the pending UI settings.

    Only fingerprints are persisted. First installation preserves existing grants;
    subsequent provider changes revoke analysis consent, not recording consent.
    """
    provider_hash = _fingerprint(provider)
    processing_hash = _fingerprint({"provider": provider_hash, "processing": processing})
    now = _now()
    with service._connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("""CREATE TABLE IF NOT EXISTS message_reading_configuration (
                        singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                        provider_hash TEXT NOT NULL, processing_hash TEXT NOT NULL,
                        updated_at TEXT NOT NULL)""")
        prior = conn.execute("SELECT * FROM message_reading_configuration WHERE singleton=1").fetchone()
        if prior and prior["processing_hash"] == processing_hash:
            conn.commit()
            return
        if prior:
            provider_changed = prior["provider_hash"] != provider_hash
            conn.execute("""UPDATE message_history_policies SET
                processing_revision=processing_revision+1, revision=revision+1, updated_at=?,
                analysis_epoch=analysis_epoch+CASE WHEN ? AND analysis_enabled=1 THEN 1 ELSE 0 END,
                analysis_enabled=CASE WHEN ? THEN 0 ELSE analysis_enabled END""",
                         (now, provider_changed, provider_changed))
            conn.execute("""UPDATE background_jobs SET status='cancelled',finished_at=?,updated_at=?,
                            lease_owner=NULL,lease_expires_at=NULL WHERE kind=?
                            AND status IN ('queued','retry_wait','running')""",
                         (now, now, ANALYSIS_JOB_KIND))
        conn.execute("""INSERT INTO message_reading_configuration VALUES(1,?,?,?)
                        ON CONFLICT(singleton) DO UPDATE SET provider_hash=excluded.provider_hash,
                        processing_hash=excluded.processing_hash, updated_at=excluded.updated_at""",
                     (provider_hash, processing_hash, now))
        conn.commit()
