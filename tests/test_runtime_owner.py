import multiprocessing
import time

from app.platform.runtime_owner import RuntimeOwner


def _hold_owner(path, ready):
    owner = RuntimeOwner(path)
    ready.put(owner.acquire())
    time.sleep(30)


def test_same_process_exclusion_release_and_reacquire(tmp_path):
    first, second = RuntimeOwner(tmp_path), RuntimeOwner(tmp_path)
    assert first.acquire()
    assert not second.acquire()
    first.release()
    first.release()
    assert second.acquire()
    second.release()


def test_process_lock_exclusion_and_sigkill_recovery(tmp_path):
    ctx = multiprocessing.get_context("spawn")
    ready = ctx.Queue()
    child = ctx.Process(target=_hold_owner, args=(str(tmp_path), ready))
    child.start()
    assert ready.get(timeout=5) is True
    local = RuntimeOwner(tmp_path)
    assert not local.acquire()
    child.kill()
    child.join(5)
    assert not child.is_alive()
    assert local.acquire()
    local.release()
