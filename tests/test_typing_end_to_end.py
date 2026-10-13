"""GUI command -> Windows spawn queue -> real Edge, against local HTML only."""
import asyncio
import json
import multiprocessing
import time
from queue import Empty
from types import SimpleNamespace

from core.store import Store
from gui.web_interface import WebInterface
from tests.test_web_bridge import ready_store


def _browser_worker(root, commands, results, generation):
    from playwright.async_api import async_playwright
    from worker import TypingCommands

    async def run():
        store = Store(root)
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(channel='msedge', headless=True)
            page = await browser.new_page()
            await page.set_content('''<form><label for="answer">Fixture answer</label>
                <textarea id="answer">Original:</textarea><button>Submit</button></form>
                <textarea id="other"></textarea><script>
                window.pastes=window.submits=0;
                document.addEventListener('paste',e=>{window.pastes++;e.preventDefault()});
                document.addEventListener('submit',e=>{window.submits++;e.preventDefault()});
                </script>''')
            # Only this fixture substitutes a programmatic click for a human click.
            manager = SimpleNamespace(context=browser.contexts[0], page=page, actual_headless=False)
            service = TypingCommands(manager, store, {'typing_commands': commands,
                'typing_generation': generation})
            recorded = set()
            click_sent = False
            try:
                while store.get('desired') != 'shutdown':
                    await service.drain()
                    state = service.engine.snapshot()
                    if state['phase'] == 'selecting' and not click_sent:
                        await page.wait_for_function("()=>window.__rainManualTypingPicker?.status==='selecting'")
                        await page.locator('#answer').click()
                        # The selection task binds asynchronously. Do not wait for
                        # another click while it is finishing the first binding.
                        click_sent = True
                    if state['phase'] == 'completed' and 'completed' not in recorded:
                        recorded.add('completed')
                        results.put({'value': await page.locator('#answer').input_value(),
                            'other': await page.locator('#other').input_value(),
                            'events': await page.evaluate('[window.pastes,window.submits]')})
                    await asyncio.sleep(.02)
            finally:
                await service.close()
                await browser.close()
    asyncio.run(run())


class _QueueDog:
    def __init__(self, process, queue):
        self.process, self.queue = process, queue

    def is_alive(self):
        return self.process.is_alive()

    def submit_typing(self, command):
        self.queue.put_nowait(command)
        return True


def _wait(predicate, process, timeout=20):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        assert process.is_alive(), f'Fixture worker exited: {process.exitcode}'
        value = predicate()
        if value:
            return value
        time.sleep(.025)
    raise AssertionError('Fixture worker command timed out')


def test_bridge_spawn_queue_real_edge_pause_resume_without_duplicate_or_paste(tmp_path):
    store = ready_store(tmp_path)
    store.set('desired', 'paused')
    ctx = multiprocessing.get_context('spawn')
    commands, results = ctx.Queue(maxsize=16), ctx.Queue(maxsize=2)
    process = ctx.Process(target=_browser_worker,
        args=(str(tmp_path), commands, results, 'fixture-generation'))
    process.start()
    ui = WebInterface(store, _QueueDog(process, commands))
    text = 'Private fixture 中文 English\n第二行。'
    try:
        _wait(lambda: ui.snapshot()['typing']['available'], process)
        assert ui.action('typing_select')['ok']
        _wait(lambda: store.get('typing_status', {}).get('phase') == 'ready', process)
        token = ui.snapshot()['typing']['target']['token']
        assert ui.action('typing_start', {'token': token, 'text': text,
            'speed': 20, 'confirmed': True})['ok']
        _wait(lambda: ui.snapshot()['typing']['sent'] >= 3, process)
        assert ui.action('typing_pause')['ok']
        _wait(lambda: ui.snapshot()['typing']['phase'] == 'paused'
            and not ui.snapshot()['typing']['pending'], process)
        assert 3 <= ui.snapshot()['typing']['sent'] < len(text)
        assert not ui.action('typing_resume', {'token': 'other-target-token'})['ok']
        assert ui.action('typing_resume', {'token': token})['ok']
        _wait(lambda: store.get('typing_status', {}).get('phase') == 'completed', process)
        result = results.get(timeout=5)
        assert result == {'value': 'Original:' + text, 'other': '', 'events': [0, 0]}
        assert ui.action('typing_stop')['ok']
        _wait(lambda: store.get('typing_status', {}).get('phase') == 'stopped', process)
        assert ui.snapshot()['typing']['target'] is None
        with store.db() as db:
            persisted = json.dumps([tuple(row) for row in db.execute('SELECT key,value FROM meta')])
        assert text not in persisted and 'Private fixture' not in persisted
    finally:
        store.set('desired', 'shutdown')
        process.join(timeout=12)
        if process.is_alive():
            process.terminate()
            process.join(timeout=5)
        for queue in (commands, results):
            queue.close()
            queue.join_thread()
    assert process.exitcode == 0
