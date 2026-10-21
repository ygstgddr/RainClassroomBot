"""Regression for a live chapter/article/video/self-test sharing one name."""
import asyncio
import pytest
from browser.edge_manager import EdgeManager
from playwright.async_api import async_playwright
from tests.test_edge import config_for
from tests.mock_site import MockSite


@pytest.mark.parametrize('leaf_id,kind', [('1', 'video'), ('2', 'article')])
def test_open_task_selects_exact_leaf_despite_duplicate_titles(tmp_path, leaf_id, kind):
    async def scenario(site):
        config = config_for(tmp_path, site.url)
        config['video_path_template'] = config['article_path_template'] = ''
        browser = EdgeManager(config)
        try:
            await browser.start()
            html = '''<h2>病毒</h2>
                <div class="is-custom-leaf" id="custom-20_2" onclick="window.chosen='2'"><b>图文</b><span>病毒</span></div>
                <div class="is-custom-leaf" id="custom-20_1" onclick="window.chosen='wrong-knowledge-point'"><b>视频</b><div class="leaf-detail" onclick="event.stopPropagation();window.chosen='1'"><span>病毒</span></div><aside style="width:500px;height:80px">进度</aside></div>
                <div class="is-custom-leaf" id="custom-20_11" onclick="window.chosen='11'"><span>病毒</span></div>
                <div class="is-custom-leaf" id="custom-20_self_test" onclick="window.chosen='test'"><span>病毒</span></div>'''
            await browser.context.route('**/v2/web/studentLog/**', lambda route: route.fulfill(body=html, content_type='text/html; charset=utf-8'))
            await browser.open_task(dict(kind=kind, classroom_id='10', leaf_id=leaf_id, title='病毒'))
            assert await browser.page.evaluate('window.chosen') == leaf_id
        finally:
            await browser.close()
    with MockSite() as site:
        asyncio.run(scenario(site))


def test_identity_across_frames_collapsed_ancestors_and_ambiguous_fallback():
    async def scenario():
        async with async_playwright() as engine:
            context = await engine.chromium.launch_persistent_context('', channel='msedge', headless=True)
            try:
                page = context.pages[0]
                browser = EdgeManager({})
                browser.context, browser.page = context, page
                await page.set_content('<a onclick="window.chosen=\'wrong\'">病毒</a><iframe></iframe>')
                await page.frames[1].set_content('''
                    <div role="treeitem" aria-expanded="false">
                        <div class="el-tree-node__content"><span class="el-tree-node__expand-icon"
                            onclick="this.parentElement.parentElement.setAttribute('aria-expanded','true');document.querySelector('#group').hidden=false">展开</span></div>
                        <div id="group" hidden><div class="is-custom-leaf" id="custom-20_1"
                            onclick="parent.chosen='right'"><span>病毒</span></div></div>
                    </div>''')
                task = dict(leaf_id='1', title='病毒')
                entry = await browser.task_entry(task)
                assert entry is not None
                await entry.click()
                assert await page.evaluate('window.chosen') == 'right'
                # Matching ID with a mismatched name must not fall back to another row.
                await page.frames[1].locator('.is-custom-leaf span').evaluate("e=>e.textContent='其他视频'")
                assert await browser.task_entry(task) is None
                # A hidden preloaded iframe must not block a unique visible legacy link.
                await page.locator('iframe').evaluate("e=>e.hidden=true")
                entry = await browser.task_entry(task)
                await entry.click()
                assert await page.evaluate('window.chosen') == 'wrong'
                await page.set_content('<span>病毒</span><span>病毒</span>')
                assert await browser.task_entry(task) is None
            finally:
                await context.close()
    asyncio.run(scenario())
