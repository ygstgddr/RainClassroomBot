"""Real Edge rendering and bridge interactions using fictional data only."""
import asyncio
from playwright.async_api import async_playwright
from core.store import Store
from gui.web_assets import load_document
from gui.web_interface import WebInterface
from main import demo_data


async def open_ui(browser, store):
    bridge = WebInterface(store)
    page = await browser.new_page(viewport={'width': 1280, 'height': 850})
    await page.expose_function('testSnapshot', bridge.snapshot)
    await page.expose_function('testAction', bridge.action)
    bootstrap = '<script>window.pywebview={api:{snapshot:()=>window.testSnapshot(),action:(n,p)=>window.testAction(n,p)}};</script>'
    await page.set_content(load_document().replace('<head>', '<head>' + bootstrap, 1), wait_until='load')
    await page.wait_for_function("window.RainUI && document.querySelectorAll('.course-card').length===2")
    return page, bridge


def test_web_selection_primary_pause_and_search(tmp_path):
    async def scenario():
        store = Store(tmp_path); demo_data(store); store.set('desired', 'idle')
        async with async_playwright() as engine:
            browser = await engine.chromium.launch(channel='msedge', headless=True)
            try:
                page, bridge = await open_ui(browser, store)
                await page.locator('.course-card').first.click()
                await page.wait_for_function('window.RainUI.getState().snapshot.counts.selected===1')
                assert len(store.selected_courses()) == 1
                await page.locator('#primary-button').click()
                await page.wait_for_function("window.RainUI.getState().snapshot.state.primary.action==='pause'")
                assert store.get('desired') == 'run'
                await page.locator('#primary-button').click()
                await page.wait_for_function("window.RainUI.getState().snapshot.state.phase==='paused'")
                assert store.get('desired') == 'paused'
                await page.locator('[data-page="tasks"]').click()
                await page.locator('#task-search').fill('不存在的任务名称')
                await page.wait_for_function("document.querySelectorAll('[data-testid=task-row]').length===0")
                await page.locator('#task-search').fill('')
                await page.wait_for_function("document.querySelectorAll('[data-testid=task-row]').length===2")
            finally:
                await browser.close()
    asyncio.run(scenario())


def test_real_glass_animation_reduced_motion_and_safe_text(tmp_path):
    async def scenario():
        store = Store(tmp_path); demo_data(store); store.set('desired', 'idle')
        async with async_playwright() as engine:
            browser = await engine.chromium.launch(channel='msedge', headless=True)
            try:
                page, bridge = await open_ui(browser, store)
                errors = []; page.on('pageerror', lambda error: errors.append(str(error)))
                await page.wait_for_timeout(400)
                glass = await page.locator('.sidebar').evaluate('el=>({background:getComputedStyle(el).backgroundColor,blur:getComputedStyle(el).backdropFilter})')
                assert 'rgba' in glass['background'] and 'blur' in glass['blur']
                card = page.locator('.course-card').first
                before = await card.evaluate('el=>getComputedStyle(el).transform')
                await card.hover(); await page.wait_for_timeout(250)
                assert before != await card.evaluate('el=>getComputedStyle(el).transform')
                snapshot = bridge.snapshot()
                attack = '<img src=x onerror="window.BAD_UI=true">'
                snapshot['courses'][0]['name'] = attack
                await page.evaluate('data=>window.RainUI.applySnapshot(data)', snapshot)
                assert attack in await page.locator('.course-card').first.inner_text()
                assert not await page.evaluate('!!window.BAD_UI')
                search = page.locator('#course-search'); await search.fill('img')
                await page.evaluate('window.keptSearch=document.querySelector("#course-search")')
                snapshot['state']['message'] = '状态已更新'
                await page.evaluate('data=>window.RainUI.applySnapshot(data)', snapshot)
                assert await search.input_value() == 'img'
                assert await page.evaluate('window.keptSearch===document.querySelector("#course-search")')
                await page.emulate_media(reduced_motion='reduce')
                motion = await page.locator('.orb').first.evaluate('el=>({name:getComputedStyle(el).animationName,seconds:parseFloat(getComputedStyle(el).animationDuration),iterations:getComputedStyle(el).animationIterationCount})')
                assert motion['name'] == 'none' or (motion['seconds'] < .02 and motion['iterations'] == '1')
                await page.set_viewport_size({'width': 900, 'height': 660})
                assert not await page.evaluate('document.documentElement.scrollWidth>innerWidth')
                assert not errors
            finally:
                await browser.close()
    asyncio.run(scenario())
