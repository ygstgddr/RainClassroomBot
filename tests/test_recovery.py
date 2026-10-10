import asyncio
import time
import psutil
from core.store import Store
from core.config import load_config
from core.logging_setup import make_logger
from core.errors import RetryableError
from task.scheduler import run_queue
from recovery.watchdog import Watchdog
from tests.test_core import sample


def live_worker(config):
    store = Store(config['root'])
    while store.get('desired') != 'shutdown':
        store.set('heartbeat', time.time())
        store.set('page_heartbeat', time.time())
        time.sleep(.1)


def unresponsive_worker(config):
    time.sleep(60)


def wait_until(predicate, timeout=12):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return
        time.sleep(.1)
    raise AssertionError('等待条件超时')


def test_watchdog_restarts_crashed_worker_preserving_task(tmp_path):
    config = load_config()
    config.update(root=str(tmp_path), watchdog_timeout_seconds=4, max_restarts_per_hour=2)
    store = Store(tmp_path)
    store.set('desired', 'run')
    store.merge_course('a', '10', [sample()])
    store.update('a:10:1', state='running', attempts=1)
    dog = Watchdog(config, worker_target=live_worker)
    dog.start()
    try:
        wait_until(lambda: store.get('worker_pid'))
        pid = store.get('worker_pid')
        wait_until(lambda: store.get('heartbeat', 0) > psutil.Process(pid).create_time())
        psutil.Process(pid).kill()
        wait_until(lambda: store.get('worker_pid') != pid)
        assert store.rows('a')[0]['state'] == 'pending'
        assert store.rows('a')[0]['attempts'] == 1
        assert store.get('desired') == 'run'
    finally:
        dog.stop()
        dog.join(10)
    assert not dog.is_alive()


def test_watchdog_heartbeat_timeout_then_restart_budget(tmp_path):
    config = load_config()
    config.update(root=str(tmp_path), watchdog_timeout_seconds=1, max_restarts_per_hour=1)
    store = Store(tmp_path)
    store.set('desired', 'run')
    dog = Watchdog(config, worker_target=unresponsive_worker)
    dog.start()
    try:
        wait_until(lambda: store.get('desired') == 'paused', timeout=15)
        assert '频繁崩溃' in store.get('worker_status')
        assert store.get('scan_progress')['status'] == 'failed'
    finally:
        dog.stop()
        dog.join(10)
    assert not dog.is_alive()


def test_failed_task_halts_queue_instead_of_advancing(tmp_path, monkeypatch):
    calls = []
    async def fail(task, *args):
        calls.append(task['leaf_id'])
        raise RetryableError('服务器未记录')
    monkeypatch.setattr('task.scheduler.execute', fail)
    class Adapter:
        async def account(self):
            return {'id': 'a'}
        async def courses(self):
            return []
    config = load_config()
    config.update(root=str(tmp_path), max_task_attempts=1)
    store = Store(tmp_path)
    store.set('desired', 'run')
    store.set('account', {'id': 'a'})
    store.select_courses(['10'], 'a')
    store.set('scanned_account', 'a')
    store.set('scanned_courses', ['10'])
    store.merge_course('a', '10', [sample(), sample(leaf='2')])
    asyncio.run(run_queue(Adapter(), None, store, config, make_logger(tmp_path, 'test_failed')))
    assert calls == ['1']
    assert store.get('desired') == 'paused'
    assert [t['state'] for t in store.rows()] == ['failed', 'pending']
    store.set('desired', 'run')
    asyncio.run(run_queue(Adapter(), None, store, config, make_logger(tmp_path, 'test_failed')))
    assert calls == ['1']  # 直接点继续不能绕过尚未解决的失败任务。
