import asyncio
from pathlib import Path
from playwright.async_api import async_playwright
from tests.test_tools_frontend import display_snapshot, open_page, target


def test_task_pages_hide_unopened_homework_and_run_clicked_order():
    async def scenario():
        snapshot, actions = display_snapshot(), []
        snapshot['tasks'] = [dict(id=str(i), title=title, course_name='演示课程', type=kind, status=status,
            type_label=label, progress=0) for i, title, kind, status, label in [
                (1, '视频一', 'video', 'pending', '视频观看'),
                (2, '课件二', 'slides', 'pending', 'PPT / 课件'),
                (3, '已开放作业', 'homework', 'manual', '作业提交'),
                (4, '未开放作业', 'homework', 'locked', '作业提交'),
                (5, '需适配视频', 'video', 'needs_adapter', '视频观看')]]
        async with async_playwright() as engine:
            browser = await engine.chromium.launch(channel='msedge', headless=True)
            try:
                page = await open_page(browser, snapshot, actions)
                errors = []; page.on('pageerror', lambda e: errors.append(str(e)))
                await page.locator('[data-page=tasks]').click()
                assert await page.locator('.task-row').count() == 2
                await page.locator('[data-id="2"] .task-pick').check()
                await page.locator('[data-id="1"] .task-pick').check()
                await page.locator('#run-selected').click()
                await page.wait_for_function('!window.RainUI.getState().pending')
                assert actions[-1] == ('start_tasks', {'ids': ['2', '1']})
                await page.locator('[data-id="1"] [data-action=run_task]').click()
                await page.wait_for_function('!window.RainUI.getState().pending')
                assert actions[-1] == ('start_tasks', {'ids': ['1']})
                await page.locator('[data-task-group=manual]').click()
                assert await page.locator('.task-row').count() == 2
                assert '未开放作业' not in await page.locator('#task-list').inner_text()
                assert await page.locator('#task-selection-bar').is_hidden()
                snapshot['tasks'][3]['status'] = 'manual'
                await page.evaluate('s=>window.RainUI.applySnapshot(s)', snapshot)
                assert await page.locator('.task-row').count() == 3
                await page.locator('[data-task-group=automatic]').click()
                out = Path(__file__).resolve().parents[3] / 'work/v087-preview'
                out.mkdir(parents=True, exist_ok=True)
                for width, height in [(1280, 850), (900, 660)]:
                    await page.set_viewport_size({'width': width, 'height': height})
                    assert not await page.evaluate('document.documentElement.scrollWidth>innerWidth')
                    await page.locator('#task-list').scroll_into_view_if_needed()
                    await page.screenshot(path=str(out/f'tasks-{width}.png'))
                assert not errors
            finally:
                await browser.close()
    asyncio.run(scenario())


def test_second_paragraph_reuses_confirmed_target_and_can_unlock():
    async def scenario():
        snapshot, actions = display_snapshot(), []
        snapshot['typing'].update(phase='ready', target=target())
        async with async_playwright() as engine:
            browser = await engine.chromium.launch(channel='msedge', headless=True)
            try:
                page = await open_page(browser, snapshot, actions)
                await page.locator('[data-page=tools]').click()
                await page.locator('#typing-source').fill('第一段')
                await page.locator('#typing-confirm').click()
                await page.locator('#typing-start').click()
                await page.wait_for_function('!window.RainUI.getState().pending')
                snapshot['typing'].update(phase='completed', sent=3, total=3)
                await page.evaluate('s=>window.RainUI.applySnapshot(s)', snapshot)
                assert await page.locator('#typing-confirm').is_hidden()
                assert await page.locator('#typing-start').inner_text() == '输入下一段'
                await page.locator('#typing-source').fill('第二段')
                await page.locator('#typing-start').click()
                await page.wait_for_function('!window.RainUI.getState().pending')
                assert [a[1]['text'] for a in actions if a[0]=='typing_start'] == ['第一段', '第二段']
                assert all(a[1]['token']=='first' for a in actions if a[0]=='typing_start')
                snapshot['typing']['phase'] = 'completed'
                await page.evaluate('s=>window.RainUI.applySnapshot(s)', snapshot)
                assert await page.locator('#typing-stop').inner_text() == '解除锁定'
                await page.locator('#typing-stop').click()
                await page.wait_for_function('!window.RainUI.getState().pending')
                assert await page.locator('#typing-start').is_disabled()
            finally:
                await browser.close()
    asyncio.run(scenario())
