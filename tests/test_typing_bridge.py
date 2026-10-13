"""指定文本框打字的 IPC/互锁验证；测试原文不得进入持久状态。"""
import json

import pytest

from gui.web_interface import WebInterface
from tests.test_web_bridge import ready_store


TOKEN = 'test-target-token'


class TypingDog:
    def __init__(self):
        self.commands = []
        self.alive = True

    def is_alive(self):
        return self.alive

    def submit_typing(self, command):
        self.commands.append(command)
        return self.alive


def desktop(tmp_path, phase='ready', multiline=True):
    store = ready_store(tmp_path)
    store.set('typing_status', {'phase': phase, 'generation': 'generation-a', 'connected': True,
        'target': {'token': TOKEN, 'label': '答案', 'url': 'https://example.test/editor?token=PRIVATE_URL',
            'frame': 'https://example.test', 'tag': 'textarea' if multiline else 'input', 'type': 'text',
            'multiline': multiline, 'characters': 3, 'existing': 'PRIVATE_EXISTING'},
        'sent': 0, 'total': 0, 'speed': 20, 'text': 'PRIVATE_SOURCE'})
    dog = TypingDog()
    return store, dog, WebInterface(store, dog)


def start_payload(**overrides):
    return {'token': TOKEN, 'text': 'PRIVATE_TYPED_TEXT\n下一行', 'speed': 20, 'confirmed': True, **overrides}


def test_snapshot_exposes_target_summary_but_no_source_existing_or_url_tokens(tmp_path):
    store, dog, ui = desktop(tmp_path)
    state = ui.snapshot()
    public = json.dumps(state, ensure_ascii=False)
    assert 'PRIVATE_' not in public
    assert state['typing']['can_start'] and not state['typing']['learning_busy']
    assert state['typing']['target']['multiline']
    assert not state['state']['primary']['enabled']


def test_start_sends_text_only_to_memory_queue_without_persisting_it(tmp_path):
    store, dog, ui = desktop(tmp_path)
    store.set('typing_status', {key: value for key, value in store.get('typing_status').items() if key != 'text'})
    result = ui.action('typing_start', start_payload())
    assert result['ok']
    assert dog.commands[0]['text'] == start_payload()['text']
    with store.db() as db:
        persisted = str([tuple(row) for row in db.execute('SELECT key,value FROM meta')])
    assert 'PRIVATE_TYPED_TEXT' not in persisted
    assert 'PRIVATE_TYPED_TEXT' not in json.dumps(ui.snapshot())
    assert not ui.action('typing_start', start_payload())['ok']  # 未应答不能重复输出。
    assert not ui.action('start')['ok']


@pytest.mark.parametrize('override', [
    {'token': 'other-target-token'}, {'confirmed': False}, {'confirmed': 'true'},
    {'text': ''}, {'text': 12}, {'text': 'x' * 50001}, {'speed': 0}, {'speed': 61},
    {'speed': float('nan')}, {'speed': True}, {'unknown': 1}])
def test_invalid_start_never_reaches_browser(tmp_path, override):
    store, dog, ui = desktop(tmp_path)
    assert not ui.action('typing_start', start_payload(**override))['ok']
    assert dog.commands == []


def test_single_line_password_and_stale_target_rejected(tmp_path):
    store, dog, ui = desktop(tmp_path, multiline=False)
    assert not ui.action('typing_start', start_payload())['ok']
    state = store.get('typing_status')
    state['target']['type'] = 'password'
    store.set('typing_status', state)
    assert not ui.action('typing_start', start_payload(text='plain'))['ok']
    state.update(target=None, phase='error')
    store.set('typing_status', state)
    assert not ui.action('typing_start', start_payload())['ok']
    assert not dog.commands


def test_selection_waits_for_scanning_task_and_pause_to_finish(tmp_path):
    store, dog, ui = desktop(tmp_path, phase='idle')
    for desired, busy in [('scan', True), ('run', True), ('catalog', False), ('login', False), ('paused', True)]:
        store.set('desired', desired)
        store.set('worker_busy', busy)
        assert not ui.action('typing_select')['ok']
        assert ui.snapshot()['typing']['learning_busy']
    store.set('desired', 'paused')
    store.set('worker_busy', False)
    assert ui.action('typing_select')['ok']
    assert not ui.action('start')['ok']


def test_pause_and_stop_can_interrupt_pending_start_and_resume_requires_token(tmp_path):
    store, dog, ui = desktop(tmp_path, phase='running')
    assert ui.action('typing_pause')['ok']
    assert ui.action('typing_stop')['ok']
    assert [c['action'] for c in dog.commands] == ['typing_pause', 'typing_stop']
    acknowledged = store.get('typing_status')
    acknowledged.update(phase='paused', request_id=dog.commands[-1]['request_id'])
    store.set('typing_status', acknowledged)
    assert not ui.action('typing_resume', {'token': 'other-target-token'})['ok']
    assert ui.action('typing_resume', {'token': TOKEN})['ok']


def test_worker_restart_discards_pending_command_and_target(tmp_path):
    store, dog, ui = desktop(tmp_path)
    assert ui.action('typing_start', start_payload())['ok']
    store.set('typing_status', {'phase': 'idle', 'target': None, 'generation': 'new-worker', 'connected': True})
    state = ui.snapshot()
    assert not state['typing']['pending']
    assert state['typing']['target'] is None
    assert state['state']['primary']['enabled']
    assert not ui.action('typing_resume', {'token': TOKEN})['ok']


def test_demo_typing_is_simulated_and_never_sends_to_real_browser(tmp_path):
    store, dog, ui = desktop(tmp_path, phase='idle')
    ui.demo = True
    assert ui.action('typing_select')['ok']
    token = ui.snapshot()['typing']['target']['token']
    assert ui.action('typing_start', start_payload(token=token))['ok']
    state = ui.snapshot()['typing']
    assert state['phase'] == 'completed' and state['target'] is None
    assert state['sent'] == len(start_payload()['text'])
    assert dog.commands == []
    assert 'PRIVATE_TYPED_TEXT' not in json.dumps(store.get('typing_status'))
