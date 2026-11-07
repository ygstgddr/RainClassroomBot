"""Login consent and rendering with an actual Edge renderer and fake bridge."""
import asyncio
import pytest
from playwright.async_api import async_playwright
from gui.web_assets import load_document


@pytest.mark.parametrize('viewport', [(1264, 811), (900, 660)])
def test_explicit_login_gate_and_verified_transition(viewport):
    async def scenario():
        calls = []
        snapshot = {'version': '0.8.6', 'site': {'label': '长江雨课堂', 'url': 'https://changjiang.yuketang.cn/web'},
            # Deliberately stale account/course information must never open the workspace.
            'account': {'name': '旧账号', 'logged_in': True},
            'courses': [{'id': 'old-course', 'name': '缓存课程', 'selected': True}],
            'state': {'phase': 'awaiting_login', 'login_gate': True, 'login_status': 'idle',
                      'busy': False, 'selection_locked': False}}
        async def action(name, payload):
            calls.append((name, payload))
            if name == 'login':
                snapshot['state'].update(login_status='connecting', busy=True)
            if name == 'switch_site':
                snapshot['site']['url'] = payload['url']
            return {'ok': True}
        async with async_playwright() as engine:
            browser = await engine.chromium.launch(channel='msedge', headless=True)
            try:
                page = await browser.new_page(viewport=dict(zip(('width', 'height'), viewport)))
                errors = []
                page.on('pageerror', lambda error: errors.append(str(error)))
                await page.expose_function('snapshotMock', lambda: snapshot)
                await page.expose_function('actionMock', action)
                bootstrap = '<script>window.pywebview={api:{snapshot:()=>window.snapshotMock(),action:(n,p)=>window.actionMock(n,p)}};</script>'
                await page.set_content(load_document().replace('<head>', '<head>'+bootstrap, 1))
                await page.wait_for_function("window.RainUI && !document.getElementById('login-button').disabled")
                assert calls == []
                assert await page.locator('#login-screen').is_visible()
                assert await page.locator('.app-shell').is_hidden()
                assert not await page.evaluate('document.documentElement.scrollWidth>innerWidth')
                bounds = await page.locator('#login-button').bounding_box()
                assert bounds['y'] >= 44 and bounds['y'] + bounds['height'] <= viewport[1]
                await page.locator('#login-screen [data-action="settings"]').first.click()
                assert await page.locator('#settings-overlay').is_visible()
                assert await page.locator('#settings-overlay [data-action="open_browser"]').is_hidden()
                await page.locator('#site-url').fill('https://www.yuketang.cn/v2/web')
                await page.locator('#site-submit').click()
                await page.wait_for_function("!window.RainUI.getState().pending")
                assert calls == [('switch_site', {'url': 'https://www.yuketang.cn/v2/web'})]
                assert await page.locator('.app-shell').is_hidden()
                await page.locator('#login-button').click()
                await page.wait_for_function("window.RainUI.getState().snapshot.state.login_status==='connecting'")
                assert await page.locator('#login-button').is_disabled()
                assert [name for name, _ in calls].count('login') == 1
                snapshot['state'].update(login_status='waiting', busy=False)
                await page.evaluate('(s)=>window.RainUI.applySnapshot(s)', snapshot)
                await page.wait_for_function("!window.RainUI.getState().pending")
                assert await page.locator('#login-button').inner_text() == '返回登录窗口'
                assert await page.locator('.app-shell').is_hidden()
                snapshot['state'].update(login_status='error', login_message='连接失败，请重试')
                await page.evaluate('(s)=>window.RainUI.applySnapshot(s)', snapshot)
                assert await page.locator('#login-button').inner_text() == '重新登录'
                assert await page.locator('#login-status').inner_text() == '连接失败，请重试'
                snapshot['state'].update(login_gate=False, login_status='idle', phase='idle')
                await page.evaluate('(s)=>window.RainUI.applySnapshot(s)', snapshot)
                assert await page.locator('#login-screen').is_hidden()
                assert await page.locator('.app-shell').is_visible()
                assert not errors
            finally:
                await browser.close()
    asyncio.run(scenario())
