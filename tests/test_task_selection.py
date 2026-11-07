"""An explicit subset must never widen after rescan, restart, or account change."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from core.config import load_config
from core.logging_setup import make_logger
from core.store import Store
from gui.web_interface import WebInterface
from task.scheduler import run_queue
from tests.test_core import sample


def prepared(tmp_path):
    store = Store(tmp_path)
    store.set('account', {'id': 'a'})
    store.select_courses(['10'])
    store.set('scanned_account', 'a'); store.set('scanned_courses', ['10'])
    store.set('scan_progress', {'status': 'completed'})
    store.set('desired', 'idle')
    store.merge_course('a', '10', [sample(leaf=str(i)) for i in range(1, 4)])
    return store, WebInterface(store)


def test_selected_order_persists_and_only_selected_tasks_execute(tmp_path, monkeypatch):
    store, ui = prepared(tmp_path)
    assert ui.action('start_tasks', {'ids': ['a:10:3', 'a:10:1']})['ok']
    assert store.get('run_mode') == 'resume'
    restored = Store(tmp_path)
    calls = []
    async def execute(task, *args):
        calls.append(task['key']); return 'server-confirmed'
    monkeypatch.setattr('task.scheduler.execute', execute)
    adapter = SimpleNamespace(account=AsyncMock(return_value={'id': 'a'}), courses=AsyncMock())
    browser = SimpleNamespace(page=SimpleNamespace(goto=AsyncMock()))
    config = load_config(); config['root'] = str(tmp_path)
    result = asyncio.run(run_queue(adapter, browser, restored, config, make_logger(tmp_path, 'selection')))
    assert calls == ['a:10:3', 'a:10:1'] and result['confirmed'] == 2
    assert next(t for t in restored.rows() if t['leaf_id'] == '2')['state'] == 'pending'
    assert restored.get('task_scope')['ids'] == calls
    assert ui.action('start')['ok'] and restored.get('task_scope') is None


@pytest.mark.parametrize('change', ['missing', 'locked', 'manual', 'account', 'courses', 'empty'])
def test_changed_selection_pauses_without_falling_back_to_all(tmp_path, monkeypatch, change):
    store, ui = prepared(tmp_path)
    ui.action('start_tasks', {'ids': ['a:10:1']})
    if change == 'missing': store.merge_course('a', '10', [sample(leaf='2')])
    if change in ('locked', 'manual'): store.update('a:10:1', state=change)
    if change in ('account', 'courses', 'empty'):
        scope = store.get('task_scope')
        scope[{'account':'account', 'courses':'courses', 'empty':'ids'}[change]] = 'b' if change == 'account' else []
        store.set('task_scope', scope)
    execute = AsyncMock(); monkeypatch.setattr('task.scheduler.execute', execute)
    adapter = SimpleNamespace(account=AsyncMock(return_value={'id': 'a'}), courses=AsyncMock())
    asyncio.run(run_queue(adapter, None, store, load_config(), make_logger(tmp_path, 'selection')))
    execute.assert_not_called()
    assert store.get('desired') == 'paused'


def test_selection_validation_and_resume_scope(tmp_path):
    store, ui = prepared(tmp_path)
    for payload in ({'ids': []}, {'ids': ['a:10:1', 'a:10:1']}, {'ids': ['b:10:1']}, {'ids': [1]}):
        assert not ui.action('start_tasks', payload)['ok']
    store.update('a:10:1', state='locked')
    assert not ui.action('start_tasks', {'ids': ['a:10:1']})['ok']
    store.update('a:10:2', state='failed', attempts=3)
    assert ui.action('start_tasks', {'ids': ['a:10:2']})['ok']
    assert next(t for t in store.rows() if t['leaf_id']=='2')['attempts'] == 0
    assert not ui.action('start_tasks', {'ids': ['a:10:3']})['ok']
    ui.action('pause'); ui.action('resume')
    assert store.get('task_scope')['ids'] == ['a:10:2']


def test_task_categories_do_not_delete_locked_homework(tmp_path):
    store, ui = prepared(tmp_path)
    rows = [sample(), dict(sample(leaf='2'), kind='homework', leaf_type=6, state='locked'),
            dict(sample(leaf='3'), kind='video', state='needs_adapter')]
    store.merge_course('a', '10', rows)
    shown = {t['id']: t for t in ui.snapshot()['tasks']}
    assert shown['a:10:1']['execution_group'] == 'automatic'
    assert shown['a:10:3']['execution_group'] == 'manual'
    assert shown['a:10:2']['hidden_until_open']
    assert len(store.rows()) == 3
    store.update('a:10:2', state='manual')
    assert not next(t for t in ui.snapshot()['tasks'] if t['id']=='a:10:2')['hidden_until_open']
