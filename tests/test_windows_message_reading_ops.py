"""Pure deployment-plan checks; never touches native data or QQ."""
from copy import deepcopy

from scripts.windows_message_reading_ops import ACCOUNT, GROUPS, reconcile_policies, replace_class


def test_class_overlay_preserves_unrelated_native_settings():
    old = "class Other:\n    own = True\n\nclass MessageHistoryConfig:\n    enabled = False\n\nlast = 1\n"
    fresh = "class MessageHistoryConfig:\n    enabled = True\n    reading_algorithm = 'selected'\n"
    merged = replace_class(old, fresh, "MessageHistoryConfig")
    assert merged.startswith("class Other:\n    own = True")
    assert merged.endswith("last = 1\n")
    assert "reading_algorithm = 'selected'" in merged


def test_reconcile_exact_three_groups_and_never_foreign_account():
    rows = [
        {"platform": "qq", "account_id": ACCOUNT, "conversation_type": kind,
         "conversation_id": group, "record_enabled": True, "analysis_enabled": False,
         "media_enabled": True, "revision": 1, "display_name": group, "batch_size": 20,
         "timezone": "Asia/Shanghai"}
        for kind, group in (("group", "376882037"), ("private", "2024919784"),
                            ("group", "1036840759"), ("group", "1046158144"))
    ]
    foreign = {**rows[0], "account_id": "another-account"}
    rows.append(foreign)
    changes = []

    def update(payload):
        changes.append(deepcopy(payload))
        row = next((r for r in rows if (r["account_id"], r["conversation_type"], r["conversation_id"])
                    == (payload["account_id"], payload["conversation_type"], payload["conversation_id"])), None)
        if row is None:
            row = {}
            rows.append(row)
        row.update({k: v for k, v in payload.items() if k not in ("start_from_now", "expected_revision")})
        row["revision"] = payload["expected_revision"] + 1
        return row

    reconcile_policies(lambda: rows, update)
    active = [r for r in rows if r["account_id"] == ACCOUNT and r["record_enabled"]]
    assert {r["conversation_id"] for r in active} == set(GROUPS)
    assert all(r["conversation_type"] == "group" and r["analysis_enabled"] for r in active)
    assert foreign["record_enabled"] is True
    assert all(r["start_from_now"] for r in changes if r["analysis_enabled"])
    before = len(changes)
    reconcile_policies(lambda: rows, update)
    assert len(changes) == before
