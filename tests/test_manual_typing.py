"""真实 Edge + 本地 HTML：不会访问真实作业或触发提交。"""
import asyncio
import json
import time
from types import SimpleNamespace

import pytest
from playwright.async_api import async_playwright

from browser.manual_typing import ManualTyping, ManualTypingError


async def wait_phase(session, phase, seconds=8):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if session.snapshot()['phase'] == phase:
            return session.snapshot()
        await asyncio.sleep(.02)
    raise AssertionError(session.snapshot())


async def pick(session, page, selector='#answer', frame=None):
    await session.select(timeout_seconds=5)
    frame = frame or page.main_frame
    await frame.wait_for_function("() => window.__rainManualTypingPicker?.status === 'selecting'")
    await frame.locator(selector).click()
    return (await wait_phase(session, 'ready'))['target']['token']


def run_edge(scenario, headless=True):
    async def run():
        async with async_playwright() as p:
            browser = await p.chromium.launch(channel='msedge', headless=headless)
            try:
                page = await browser.new_page()
                # 本地点击由测试驱动；产品仅允许可见专用 Edge 中人工点选。
                manager = SimpleNamespace(context=browser.contexts[0], page=page, config={'headless': False})
                session = ManualTyping(manager)
                try:
                    await scenario(page, session)
                finally:
                    await session.stop()
            finally:
                await browser.close()
    asyncio.run(run())


def test_real_native_chinese_ascii_newline_append_without_paste_submit_or_text_persistence():
    async def scenario(page, session):
        updates = []
        session.on_update = updates.append
        await page.set_content('''<form><label for="answer">作业正文</label>
          <textarea id="answer">原有文字</textarea><button>提交</button></form><textarea id="other"></textarea>
          <script>window.pastes=window.submits=window.enters=0;window.keys=[];
            document.addEventListener('paste',e=>{window.pastes++;e.preventDefault()});
            document.addEventListener('submit',e=>{window.submits++;e.preventDefault()});
            document.addEventListener('keydown',e=>{window.keys.push(e.key);if(e.key==='Enter')window.enters++});
          </script>''')
        await page.locator('#answer').evaluate('e=>{e.focus();e.select()}')
        token = await pick(session, page)
        source = '中文private answer\n第二行。'
        await session.start(token, source, speed=60, confirmed=True)
        state = await wait_phase(session, 'completed')
        assert await page.locator('#answer').input_value() == '原有文字' + source
        assert await page.locator('#other').input_value() == ''
        assert await page.evaluate('[window.pastes,window.submits,window.enters]') == [0, 0, 0]
        assert 'p' in await page.evaluate('window.keys')
        assert state['sent'] == state['total'] == len(source)
        assert source not in json.dumps(updates, ensure_ascii=False)
        assert session._text == ''
    run_edge(scenario)


@pytest.mark.parametrize('kind', ['textarea', 'contenteditable'])
def test_iframe_target_only_and_multiline(kind):
    async def scenario(page, session):
        await page.set_content('<textarea id="other"></textarea><iframe style="width:600px;height:300px"></iframe>')
        frame = page.frames[1]
        html = '<textarea id="answer"></textarea>' if kind == 'textarea' else '<div id="answer" contenteditable="true" style="height:100px;border:1px solid">已有</div>'
        await frame.set_content(html)
        token = await pick(session, page, frame=frame)
        assert session.snapshot()['target']['multiline']
        await session.start(token, '英文English\n中文', speed=60, confirmed=True)
        await wait_phase(session, 'completed')
        if kind == 'textarea':
            assert await frame.locator('#answer').input_value() == '英文English\n中文'
        else:
            assert await frame.locator('#answer').inner_text() == '已有英文English\n中文'
        assert await page.locator('#other').input_value() == ''
    run_edge(scenario)


def test_focus_race_between_validation_and_native_input_never_writes_another_box():
    async def scenario(page, session):
        await page.set_content('<textarea id="answer"></textarea><textarea id="other"></textarea>')
        token = await pick(session, page)
        original = page.keyboard.insert_text
        raced = False
        async def racing_insert(text):
            nonlocal raced
            if not raced:
                raced = True
                await page.locator('#other').focus()
            await original(text)
        page.keyboard.insert_text = racing_insert
        await session.start(token, '禁止串框', speed=60, confirmed=True)
        state = await wait_phase(session, 'paused')
        assert state['sent'] == 0
        assert await page.locator('#answer').input_value() == ''
        assert await page.locator('#other').input_value() == ''
        # 自动暂停后守卫撤销，用户可以正常手动操作其他框。
        await session._wait_runner()
        await original('正常人工输入')
        assert await page.locator('#other').input_value() == '正常人工输入'
    run_edge(scenario)


def test_cross_frame_focus_race_cannot_write_other_frame():
    async def scenario(page, session):
        await page.set_content('<textarea id="answer"></textarea><iframe style="width:600px;height:300px"></iframe>')
        other_frame = page.frames[1]
        await other_frame.set_content('<textarea id="other"></textarea>')
        token = await pick(session, page)
        original = page.keyboard.insert_text
        async def racing_insert(text):
            await other_frame.locator('#other').focus()
            await original(text)
        page.keyboard.insert_text = racing_insert
        await session.start(token, '禁止', speed=60, confirmed=True)
        await wait_phase(session, 'paused')
        assert await other_frame.locator('#other').input_value() == ''
        assert await page.locator('#answer').input_value() == ''
    run_edge(scenario)


def test_complete_navigation_after_validation_before_native_input_cannot_write_new_document():
    async def scenario(page, session):
        await page.set_content('<textarea id="answer"></textarea>')
        token = await pick(session, page)
        original = page.keyboard.insert_text
        async def navigation_race(text):
            await page.goto('data:text/html,<textarea id="replacement" autofocus></textarea>')
            await page.locator('#replacement').focus()
            await original(text)
        page.keyboard.insert_text = navigation_race
        await session.start(token, '禁止输出新页面', speed=60, confirmed=True)
        await wait_phase(session, 'error')
        assert await page.locator('#replacement').input_value() == ''
        assert session.snapshot()['target'] is None
        assert session._text == ''
        await session._wait_runner()
        # 在途输入安全结束后新页面立即恢复正常手动编辑，没有长期全局阻挡。
        await original('正常手动输入')
        assert await page.locator('#replacement').input_value() == '正常手动输入'
        await page.goto('data:text/html,<textarea id="replacement" autofocus></textarea>')
        await page.wait_for_function('() => window.__rainManualTypingNavigation?.locked === false')
        await original('继续手动输入')
        assert await page.locator('#replacement').input_value() == '继续手动输入'
    run_edge(scenario)


def test_input_handler_focus_change_resume_does_not_repeat_accepted_character():
    async def scenario(page, session):
        await page.set_content('''<textarea id="answer"></textarea><textarea id="other"></textarea>
          <script>answer.addEventListener('input',()=>other.focus(),{once:true})</script>''')
        token = await pick(session, page)
        await session.start(token, 'ABC中文', speed=60, confirmed=True)
        state = await wait_phase(session, 'paused')
        assert state['sent'] == 1
        assert await page.locator('#answer').input_value() == 'A'
        await session.resume(token)
        await wait_phase(session, 'completed')
        assert await page.locator('#answer').input_value() == 'ABC中文'
        assert await page.locator('#other').input_value() == ''
    run_edge(scenario)


@pytest.mark.parametrize('change', ['replace', 'disable', 'readonly', 'hide', 'password', 'navigate'])
def test_original_target_changes_invalidate_and_never_follow_selector(change):
    async def scenario(page, session):
        await page.set_content('<input id="answer" aria-label="正文"><input id="other">')
        token = await pick(session, page)
        await session.start(token, 'ABCDEFGHIJK', speed=2, confirmed=True)
        await page.wait_for_function("() => document.querySelector('#answer').value.length >= 1")
        if change == 'navigate':
            await page.goto('about:blank')
            await page.set_content('<input id="answer"><input id="other">')
        else:
            actions = {'replace': 'const clone=e.cloneNode();clone.value="";e.replaceWith(clone)', 'disable': 'e.disabled=true',
                       'readonly': 'e.readOnly=true', 'hide': 'e.style.display="none"',
                       'password': 'e.type="password"'}
            await page.locator('#answer').evaluate('e=>{' + actions[change] + '}')
        await wait_phase(session, 'error')
        assert session.snapshot()['target'] is None
        with pytest.raises(ManualTypingError):
            await session.resume(token)
        assert await page.locator('#other').input_value() == ''
        if change in ('replace', 'navigate'):
            assert await page.locator('#answer').input_value() == ''
    run_edge(scenario)


def test_password_and_buttons_rejected_explicit_confirmation_and_single_line_newline_validation():
    async def scenario(page, session):
        await page.set_content('''<input id="answer" aria-label="单行正文"><input id="secret" type="password" value="sensitive">
          <button id="submit" onclick="window.submits++">提交</button><script>window.submits=0</script>''')
        await session.select()
        await page.wait_for_function("() => window.__rainManualTypingPicker?.status === 'selecting'")
        await page.locator('#secret').click()
        await asyncio.sleep(.2)
        assert session.snapshot()['phase'] == 'selecting'
        await page.locator('#submit').click()
        assert await page.evaluate('window.submits') == 0
        assert 'sensitive' not in json.dumps(session.snapshot())
        await page.locator('#answer').click()
        token = (await wait_phase(session, 'ready'))['target']['token']
        with pytest.raises(ManualTypingError, match='确认'):
            await session.start(token, 'A')
        with pytest.raises(ManualTypingError, match='过期'):
            await session.start('wrong token', 'A', confirmed=True)
        with pytest.raises(ManualTypingError, match='单行'):
            await session.start(token, 'A\nB', confirmed=True)
        with pytest.raises(ManualTypingError, match='50000'):
            await session.start(token, 'A'*50001, confirmed=True)
        assert await page.locator('#answer').input_value() == ''
    run_edge(scenario)


def test_pause_stop_fast_speed_adjustment_and_no_replay():
    async def scenario(page, session):
        updates = []
        session.on_update = lambda snapshot: updates.append((time.monotonic(), snapshot))
        await page.set_content('<textarea id="answer"></textarea><textarea id="other"></textarea>')
        token = await pick(session, page)
        await session.start(token, 'ABCDEFGHIJKLMNO', speed=1, confirmed=True)
        await page.wait_for_function("() => answer.value === 'A'")
        started = time.monotonic()
        await session.pause()
        assert time.monotonic() - started < .5
        sent = session.snapshot()['sent']
        await asyncio.sleep(.1)
        assert len(await page.locator('#answer').input_value()) == sent
        await session.set_speed(60)
        await session.resume(token)
        await wait_phase(session, 'completed')
        assert await page.locator('#answer').input_value() == 'ABCDEFGHIJKLMNO'
        await session.start(token, '不应恢复的后续输入'*20, speed=1, confirmed=True)
        await asyncio.sleep(.05)
        started = time.monotonic()
        await session.stop()
        assert time.monotonic() - started < .5
        after = await page.locator('#answer').input_value()
        await asyncio.sleep(.1)
        assert await page.locator('#answer').input_value() == after
        assert session.snapshot()['target'] is None
        assert session._text == ''
        with pytest.raises(ManualTypingError):
            await session.resume(token)
        running_updates = [t for t, s in updates if s['phase'] == 'running']
        assert len(running_updates) < 10
    run_edge(scenario)


def test_maxlength_rejected_character_is_not_counted_or_repeated():
    async def scenario(page, session):
        await page.set_content('<textarea id="answer" maxlength="2"></textarea>')
        token = await pick(session, page)
        await session.start(token, 'ABCD', speed=60, confirmed=True)
        state = await wait_phase(session, 'paused')
        assert state['sent'] == 2
        assert await page.locator('#answer').input_value() == 'AB'
        assert '未接受' in state['message']
    run_edge(scenario)


def test_target_iframe_hidden_or_new_frame_added_clears_binding():
    async def scenario(page, session):
        await page.set_content('<iframe id="frame" style="width:500px;height:250px"></iframe>')
        frame = page.frames[1]
        await frame.set_content('<textarea id="answer"></textarea>')
        token = await pick(session, page, frame=frame)
        await page.locator('#frame').evaluate('e=>e.style.opacity=0')
        with pytest.raises(ManualTypingError, match='不可用'):
            await session.start(token, 'A', confirmed=True)
        assert session.snapshot()['phase'] == 'error'
        assert session.snapshot()['target'] is None
        assert await frame.locator('#answer').input_value() == ''
        await page.locator('#frame').evaluate('e=>e.style.opacity=1')
        token = await pick(session, page, frame=frame)
        await page.evaluate("() => document.body.appendChild(document.createElement('iframe'))")
        await wait_phase(session, 'error')
        assert session.snapshot()['target'] is None
        with pytest.raises(ManualTypingError):
            await session.start(token, 'A', confirmed=True)
    run_edge(scenario)


def test_label_does_not_include_wrapped_existing_text_and_headless_picker_refused():
    async def scenario(page, session):
        await page.set_content('<label>作业正文<textarea id="answer">existing private answer</textarea></label>')
        await pick(session, page)
        assert session.snapshot()['target']['label'] == '作业正文'
        assert 'existing private answer' not in json.dumps(session.snapshot())
        session.browser.actual_headless = True
        with pytest.raises(ManualTypingError, match='可见'):
            await session.select()
        assert session.snapshot()['target'] is None
    run_edge(scenario)


def test_headed_other_window_start_and_resume_only_raise_original_bound_page():
    async def scenario(page, session):
        await page.set_content('<textarea id="answer"></textarea>')
        token = await pick(session, page)
        other_context = await page.context.browser.new_context()
        try:
            other = await other_context.new_page()
            await other.set_content('<textarea id="other"></textarea>')
            await other.bring_to_front()
            await other.locator('#other').click()
            calls = []
            original = page.bring_to_front
            async def recorded_front():
                calls.append(session.snapshot()['phase'])
                await original()
            page.bring_to_front = recorded_front
            await session.start(token, 'ABC中文', speed=2, confirmed=True)
            assert calls == ['ready']
            await page.wait_for_function("() => answer.value === 'A'")
            await session.pause()
            await other.bring_to_front()
            await other.locator('#other').click()
            await session.set_speed(60)
            await session.resume(token)
            await wait_phase(session, 'completed')
            # 仅用户明确 start/resume 时展示原绑定窗口；输出期间不反复抢焦点。
            assert calls == ['ready', 'paused']
            assert await page.locator('#answer').input_value() == 'ABC中文'
            assert await other.locator('#other').input_value() == ''
        finally:
            await other_context.close()
    run_edge(scenario, headless=False)
