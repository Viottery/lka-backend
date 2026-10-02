"""The registered Agent mail listing contract, without a live mailbox or LLM."""

from types import SimpleNamespace

from app.api.main import create_app
from app.api.routes.mail import import_mail
from app.api.schemas import MailImportRequest
from app.core.config import get_settings
from app.core.tools import ToolContext


def test_registered_mail_list_pages_without_exposing_bodies(tmp_path, monkeypatch):
    monkeypatch.setenv("LKA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("LKA_LOCAL_CONFIG", str(tmp_path / "missing-local.toml"))
    get_settings.cache_clear()
    app = create_app()
    import_mail(
        MailImportRequest(
            account={"provider": "local_json", "email_address": "user@example.com"},
            messages=[
                {
                    "external_id": f"mail-{index}",
                    "subject": f"Subject {index}",
                    "sender": "sender@example.com",
                    "received_at": "2026-09-25T10:00:00Z",
                    "body_text": f"private body {index}",
                }
                for index in range(25)
            ],
        ),
        SimpleNamespace(app=app),
    )
    context = ToolContext(session_id="mail-list", trace_id="mail-list")
    filters = {
        "received_from": "2026-09-23T00:00:00Z",
        "received_before": "2026-09-30T00:00:00Z",
        "folder": "inbox",
    }
    first = app.state.runtime.tool_executor.execute(
        invocation_id="mail-list-first", tool_name="mail.list", tool_input=filters,
        context=context,
    )
    assert first.status == "completed"
    assert first.output["total_matches"] == 25
    assert first.output["returned_count"] == 20
    assert first.output["next_range"] == {"start_rank": 21, "end_rank": 25}

    second = app.state.runtime.tool_executor.execute(
        invocation_id="mail-list-second", tool_name="mail.list",
        tool_input={**filters, **first.output["next_range"], "listing_id": first.output["listing_id"]},
        context=context,
    )
    assert second.status == "completed"
    assert second.output["returned_count"] == 5
    assert second.output["has_more"] is False
    cards = first.output["messages"] + second.output["messages"]
    assert len({card["message_id"] for card in cards}) == 25
    assert all("body_text" not in card and "snippet" not in card for card in cards)
