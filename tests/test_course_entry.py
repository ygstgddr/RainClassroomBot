import asyncio
import pytest
from browser.edge_manager import EdgeManager
from core.errors import LoginRequired
from tests.test_edge import config_for
from tests.mock_site import MockSite
from browser.visibility import visible_security_challenge
from playwright.async_api import async_playwright


@pytest.mark.parametrize('challenge', [False, True])
def test_course_content_tab_and_security_pause(tmp_path, challenge):
    async def scenario(site):
        config = config_for(tmp_path, site.url)
        config['video_path_template'] = ''
        browser = EdgeManager(config)
        try:
            await browser.start()
            iframe = ('<iframe src="/template/drag_ele.html"></iframe>' if challenge else
                      '<iframe srcdoc="&lt;a href=\'/play-video\'&gt;测试视频&lt;/a&gt;"></iframe>')
            html = ('<div class="rain-tabs__nav-item" onclick="document.querySelector(\'#content\').hidden=false">'
                    '学习内容</div><div id="content" hidden>' + iframe + '</div>')
            await browser.context.route('**/v2/web/studentLog/**', lambda route: route.fulfill(body=html, content_type='text/html; charset=utf-8'))
            await browser.context.route('**/template/drag_ele.html', lambda route: route.fulfill(body='安全验证', content_type='text/html; charset=utf-8'))
            await browser.context.route('**/play-video', lambda route: route.fulfill(body='<video controls></video>', content_type='text/html; charset=utf-8'))
            task = dict(kind='video', classroom_id='10', leaf_id='1', title='测试视频')
            if challenge:
                with pytest.raises(LoginRequired, match='安全验证'):
                    await browser.open_task(task)
                # 模拟用户完成验证；再次进入任务不能重载原页面而重新触发验证。
                await browser.page.evaluate("document.querySelector('iframe').remove();document.querySelector('#content').innerHTML='<a href=\"/play-video\">测试视频</a>'")
                await browser.open_task(task)
                assert browser.page.url.endswith('/play-video')
            else:
                await browser.open_task(task)
                assert any(f.url.endswith('/play-video') for f in browser.page.frames)
                assert await browser.page.locator('#content').is_visible()
        finally:
            await browser.close()
    with MockSite() as site:
        asyncio.run(scenario(site))


@pytest.mark.parametrize('style', [
    'position:absolute;top:-1000000px;opacity:0',
    'opacity:0', 'position:absolute;left:-10000px', 'display:none',
])
def test_preloaded_hidden_security_frame_is_not_a_challenge(style):
    async def scenario():
        async with async_playwright() as p:
            browser=await p.chromium.launch(channel='msedge',headless=True)
            try:
                page=await browser.new_page()
                await page.route('**/drag_ele.html',lambda r:r.fulfill(
                    body='<span>安全验证</span>',content_type='text/html; charset=utf-8'))
                await page.set_content(f'<div style="{style}"><iframe src="https://test.invalid/drag_ele.html"></iframe></div>')
                frame=page.frames[1]
                await frame.wait_for_load_state()
                assert not await visible_security_challenge(frame)
                # 同一个 iframe 变为真正可见时才应报告验证。
                await page.locator('div').evaluate("e=>e.removeAttribute('style')")
                assert await visible_security_challenge(frame)
            finally:
                await browser.close()
    asyncio.run(scenario())
