"""工作进程只消费当前 generation 的明确动作，结束后释放原文引用。"""
import asyncio
from queue import Queue

from core.store import Store
from worker import TypingCommands


class FakeTyping:
    def __init__(self, browser, on_update):
        self.on_update = on_update
        self.state = {'phase': 'idle', 'target': None, 'speed': 20}
        self.calls = []

    def snapshot(self):
        return dict(self.state)

    async def select(self):
        self.calls.append('select')
        self.state.update(phase='ready', target={'token': 'target-token', 'tag': 'textarea',
                         'multiline': True, 'label': '答案', 'existing': 'PRIVATE_EXISTING'})
        self.on_update(self.state)

    async def start(self, token, text, speed, confirmed):
        self.calls.append(('start', token, text, speed, confirmed))
        self.state.update(phase='running', total=len(text), source='PRIVATE_SOURCE')
        self.on_update(self.state)

    async def pause(self):
        self.calls.append('pause')
        self.state['phase'] = 'paused'

    async def resume(self, token):
        self.calls.append(('resume', token))
        self.state['phase'] = 'running'

    async def set_speed(self, speed):
        self.state['speed'] = speed

    async def cancel(self):
        await self.stop()

    async def stop(self):
        self.calls.append('stop')
        self.state.update(phase='stopped', target=None)


def setup(tmp_path):
    store = Store(tmp_path)
    store.set('desired', 'paused')
    queue = Queue()
    service = TypingCommands(None, store, {'typing_commands': queue, 'typing_generation': 'current'}, FakeTyping)
    return store, queue, service


def command(action, **kwargs):
    return {'action': action, 'generation': 'current', 'request_id': action, **kwargs}


def test_queue_control_order_and_target_whitelist(tmp_path):
    store, queue, service = setup(tmp_path)
    queue.put(command('typing_select'))
    queue.put(command('typing_start', token='target-token', text='PRIVATE_USER_TEXT', speed=20, confirmed=True))
    queue.put(command('typing_pause'))
    queue.put(command('typing_resume', token='target-token'))
    queue.put(command('typing_stop'))
    asyncio.run(service.drain())
    assert service.engine.calls == ['select', ('start', 'target-token', 'PRIVATE_USER_TEXT', 20, True),
                                   'pause', ('resume', 'target-token'), 'stop']
    assert store.get('typing_status')['phase'] == 'stopped'
    with store.db() as db:
        persisted = str([tuple(row) for row in db.execute('SELECT key,value FROM meta')])
    assert 'PRIVATE_' not in persisted
    assert queue.empty()


def test_old_worker_generation_never_replays_and_consumed_payload_is_cleared(tmp_path):
    store, queue, service = setup(tmp_path)
    stale = command('typing_start', generation='old-worker', text='PRIVATE_OLD_TEXT')
    current = command('typing_select')
    queue.put(stale)
    queue.put(current)
    asyncio.run(service.drain())
    assert service.engine.calls == ['select']
    assert current == {}
    assert store.get('typing_status')['generation'] == 'current'


def test_active_learning_and_pause_in_flight_block_selection_but_not_stop(tmp_path):
    for desired, busy in [('run', True), ('catalog', False), ('scan', True), ('login', False), ('paused', True)]:
        store, queue, service = setup(tmp_path)
        store.set('desired', desired)
        store.set('worker_busy', busy)
        queue.put(command('typing_select'))
        queue.put(command('typing_stop'))
        asyncio.run(service.drain())
        assert service.engine.calls == ['stop']


def test_close_disables_typing_and_requires_new_binding(tmp_path):
    store, queue, service = setup(tmp_path)
    queue.put(command('typing_select'))
    asyncio.run(service.drain())
    assert service.active
    asyncio.run(service.close())
    state = store.get('typing_status')
    assert not state['connected'] and state['target'] is None and state['phase'] == 'idle'
