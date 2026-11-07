"""Real Edge renders the update UI; bridge responses are local fixtures."""
import asyncio
from pathlib import Path
from playwright.async_api import async_playwright
from tests.test_tools_frontend import display_snapshot, open_page


def test_update_flow_and_compact_layout_without_learning_login():
    async def scenario():
        snapshot, actions = display_snapshot(), []
        snapshot['version'] = '0.8.7'
        snapshot['update'] = dict(phase='idle', current='0.8.7', install_supported=True, can_install=True)
        async with async_playwright() as engine:
            browser = await engine.chromium.launch(channel='msedge', headless=True)
            try:
                page = await open_page(browser, snapshot, actions)
                errors = []
                page.on('pageerror', lambda err: errors.append(str(err)))
                snapshot['state'].update(login_gate=True, login_status='idle')
                await page.evaluate('s=>RainUI.applySnapshot(s)', snapshot)
                await page.locator('#login-screen [data-action=settings]').first.click()
                assert await page.locator('#update-check').is_enabled()
                await page.locator('#update-check').click()
                await page.wait_for_function('!RainUI.getState().pending')
                assert actions[-1] == ('update_check', {})
                assert await page.locator('#site-error').inner_text() == ''
                snapshot['update'].update(phase='available', latest='0.9.0', notes='<img src=x onerror=alert(1)>\n更新说明', can_download=True)
                await page.evaluate('s=>RainUI.applySnapshot(s)', snapshot)
                await page.locator('#update-details summary').click()
                assert await page.locator('#update-notes img').count() == 0
                assert '更新说明' in await page.locator('#update-notes').inner_text()
                await page.locator('#update-download').click()
                await page.wait_for_function('!RainUI.getState().pending')
                assert actions[-1] == ('update_download', {})
                snapshot['update'].update(phase='downloading', can_download=False, downloaded=524288, total=1048576)
                await page.evaluate('s=>RainUI.applySnapshot(s)', snapshot)
                assert await page.locator('#update-progress').evaluate('(el)=>el.value') == 50
                assert await page.locator('#update-check').is_disabled()
                await page.locator('#update-cancel').click()
                await page.wait_for_function('!RainUI.getState().pending')
                assert actions[-1] == ('update_cancel', {})
                snapshot['update'].update(phase='ready', can_install=False)
                await page.evaluate('s=>RainUI.applySnapshot(s)', snapshot)
                assert await page.locator('#update-install').is_disabled()
                assert '暂停' in await page.locator('#update-hint').inner_text()
                snapshot['update']['can_install'] = True
                await page.evaluate('s=>RainUI.applySnapshot(s)', snapshot)
                await page.locator('#update-install').click()
                await page.wait_for_function('!RainUI.getState().pending')
                assert actions[-1] == ('update_install', {})
                snapshot['update'].update(phase='available', can_download=True, message='发现新版本，可下载后安装', notes='新增软件内更新\n支持进度查看与下载取消\n保留登录状态与任务记录')
                await page.evaluate('s=>RainUI.applySnapshot(s)', snapshot)
                await page.evaluate("document.documentElement.classList.add('desktop-glass')")
                output = Path(__file__).resolve().parents[3] / 'work/update-preview'
                output.mkdir(parents=True, exist_ok=True)
                for width, height in [(1264, 811), (900, 660)]:
                    await page.set_viewport_size({'width': width, 'height': height})
                    await page.locator('#update-check').scroll_into_view_if_needed()
                    assert not await page.evaluate('document.documentElement.scrollWidth > innerWidth')
                    assert await page.locator('#update-check').is_visible()
                    await page.screenshot(path=str(output / f'update-{width}.png'))
                snapshot['update'].update(phase='ready', install_supported=False)
                await page.evaluate('s=>RainUI.applySnapshot(s)', snapshot)
                assert await page.locator('#update-install').is_disabled()
                assert '源码' in await page.locator('#update-hint').inner_text()
                assert not errors
            finally:
                await browser.close()
    asyncio.run(scenario())
