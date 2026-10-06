from scripts.eval_message_reading import replay


def test_fixed_310_messages_replay_counts_signals_and_no_model_authorization(tmp_path):
    from app.core.background_jobs import BackgroundJobStore
    from app.domains.message_history import MessageHistoryService
    jobs = BackgroundJobStore(tmp_path / "messages.sqlite")
    jobs.ensure_schema()
    service = MessageHistoryService(tmp_path / "messages.sqlite", jobs)
    service.ensure_schema()
    result = replay(service)
    assert result["passed"], result
    assert result["semantic_quality_evaluated"] is False
