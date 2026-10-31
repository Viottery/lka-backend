"""Notification-driven streams retain paging and cannot miss a read/wait race."""
import asyncio
from types import SimpleNamespace

from app.api.routes.agent import _agent_turn_event_stream
from app.core.agent_runs import InMemoryAgentRunManager
from app.core.run_change_signals import RunChangeSignals


def test_signals_are_scoped_bounded_and_cleanup_is_idempotent():
    signals = RunChangeSignals(limit=2)
    calls = []
    callback = lambda: calls.append('a')
    first = signals.subscribe('a', callback)
    second = signals.subscribe('a', callback)
    assert signals.subscribe('b', lambda: None) is None
    signals.notify('b')
    assert calls == []
    signals.notify('a')
    assert calls == ['a', 'a']
    first()
    first()
    signals.notify('a')
    assert calls == ['a', 'a', 'a']
    signals.close()
    second()
    assert not signals._listeners
    assert signals._count == 0
    assert signals.subscribe('a', callback) is None


def _request(manager):
    async def connected():
        return False
    return SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(runtime=SimpleNamespace(agent_run_manager=manager))),
        is_disconnected=connected,
    )


def test_idle_stream_waits_for_state_notification_and_cleans_subscription():
    async def scenario():
        manager = InMemoryAgentRunManager()
        run = manager.create_run(session_id='s', user_input='synthetic')
        manager.mark_running(run.run_id)
        original = manager.list_events
        reads = []
        def counted(*args, **kwargs):
            reads.append(1)
            return original(*args, **kwargs)
        manager.list_events = counted
        async def consume():
            return [frame async for frame in _agent_turn_event_stream(request=_request(manager), run_id=run.run_id)]
        task = asyncio.create_task(consume())
        await asyncio.sleep(.22)
        assert len(reads) == 1  # Prior polling reads every 50ms while idle.
        manager.complete_run(run.run_id)
        assert await asyncio.wait_for(task, 1) == []
        assert not manager.change_signals._listeners
        manager.close()
    asyncio.run(scenario())


def test_event_between_empty_read_and_wait_is_not_lost():
    async def scenario():
        manager = InMemoryAgentRunManager()
        run = manager.create_run(session_id='s', user_input='synthetic')
        manager.mark_running(run.run_id)
        original = manager.list_events
        injected = False
        def racing(*args, **kwargs):
            nonlocal injected
            result = original(*args, **kwargs)
            if not injected:
                injected = True
                manager.append_event(run.run_id, 'progress', 'new')
            return result
        manager.list_events = racing
        iterator = _agent_turn_event_stream(request=_request(manager), run_id=run.run_id)
        frame = await asyncio.wait_for(anext(iterator), 1)
        assert 'progress' in frame
        await iterator.aclose()
        assert not manager.change_signals._listeners
        manager.close()
    asyncio.run(scenario())


def test_shutdown_wakes_an_idle_stream():
    async def scenario():
        manager = InMemoryAgentRunManager()
        run = manager.create_run(session_id='s', user_input='synthetic')
        task = asyncio.create_task(anext(_agent_turn_event_stream(request=_request(manager), run_id=run.run_id), None))
        await asyncio.sleep(.03)
        manager.close()
        assert await asyncio.wait_for(task, 1) is None
        assert not manager.change_signals._listeners
    asyncio.run(scenario())
