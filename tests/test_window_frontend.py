"""Real Edge checks for the integrated frame with fictional native actions."""
import asyncio
from copy import deepcopy

from playwright.async_api import async_playwright

from gui.web_assets import load_document


def test_browser_preview_window_buttons_are_disabled_and_brand_is_local():
    async def scenario():
        async with async_playwright() as engine:
            browser = await engine.chromium.launch(channel="msedge", headless=True)
            try:
                page = await browser.new_page(viewport={"width": 900, "height": 660})
                document = load_document().replace("<head>", "<head><script>window.__RAIN_DEMO__=true</script>", 1)
                await page.set_content(document)
                await page.wait_for_function("window.RainUI && document.querySelectorAll('.course-card').length===6")
                for name in ("close", "minimize", "maximize"):
                    button = page.locator("#window-" + name)
                    assert await button.is_disabled()
                    assert "桌面程序" in await button.get_attribute("title")
                before = await page.evaluate("window.RainUI.getState().snapshot.state.phase")
                await page.locator("#window-drag-region").dblclick()
                assert await page.evaluate("window.RainUI.getState().snapshot.state.phase") == before
                assert "演示" in await page.locator("#window-caption").inner_text()
                for image in (".brand-mark img", ".window-title-logo"):
                    assert await page.locator(image).evaluate("el=>el.complete && el.naturalWidth===64 && el.src.startsWith('data:image/png;base64,')")
                layout = await page.evaluate("""({overflow:document.documentElement.scrollWidth>innerWidth,
                    bar:document.getElementById('window-titlebar').getBoundingClientRect().bottom,
                    workspace:document.querySelector('.app-shell').getBoundingClientRect().top,
                    bottom:document.querySelector('.app-shell').getBoundingClientRect().bottom})""")
                assert not layout["overflow"] and layout["bar"] == layout["workspace"] == 44
                assert layout["bottom"] <= 660
                controls = await page.locator(".window-control").evaluate_all("els=>els.map(el=>({id:el.id,x:el.getBoundingClientRect().x,right:el.getBoundingClientRect().right}))")
                assert [item["id"] for item in controls] == ["window-minimize", "window-maximize", "window-close"]
                assert all(item["x"] > 800 and item["right"] <= 900 for item in controls)
                assert controls[0]["x"] < controls[1]["x"] < controls[2]["x"]
                title_center = await page.locator("#window-drag-region").evaluate("el=>{const r=el.getBoundingClientRect();return r.x+r.width/2}")
                assert title_center == 450
            finally:
                await browser.close()
    asyncio.run(scenario())


def test_frame_controls_only_send_native_actions_and_follow_maximized_state():
    async def scenario():
        snapshot = {
            "version": "0.8.0", "window": {"maximized": False, "native_controls": True},
            "state": {"phase": "running", "busy": True, "selection_locked": True,
                      "primary": {"action": "pause", "label": "暂停学习", "enabled": True}},
        }
        actions = []

        def command(name, payload):
            actions.append((name, payload))
            if name == "window_toggle_maximize":
                snapshot["window"]["maximized"] = not snapshot["window"]["maximized"]
            return {"ok": True}

        async with async_playwright() as engine:
            browser = await engine.chromium.launch(channel="msedge", headless=True)
            try:
                page = await browser.new_page(viewport={"width": 1280, "height": 850})
                await page.expose_function("frameSnapshot", lambda: deepcopy(snapshot))
                await page.expose_function("frameAction", command)
                bootstrap = '<script>window.pywebview={api:{snapshot:()=>window.frameSnapshot(),action:(n,p)=>window.frameAction(n,p)}}</script>'
                await page.set_content(load_document().replace("<head>", "<head>" + bootstrap, 1))
                await page.wait_for_function("window.RainUI && !document.getElementById('window-maximize').disabled")
                assert not await page.locator("#window-close").is_disabled()
                assert not await page.locator("#window-minimize").is_disabled()
                assert await page.locator(".window-control").first.evaluate("el=>!el.closest('.pywebview-drag-region')")
                assert await page.locator(".window-title-logo").evaluate("el=>getComputedStyle(el).pointerEvents==='none'")
                await page.locator("#window-maximize").click()
                await page.wait_for_function("document.body.classList.contains('window-is-maximized') && !document.getElementById('window-maximize').disabled")
                assert "还原" in await page.locator("#window-maximize").get_attribute("aria-label")
                assert not await page.locator(".resize-n").is_visible()
                await page.locator("#window-drag-region").dblclick()
                await page.wait_for_function("!document.body.classList.contains('window-is-maximized') && !document.getElementById('window-maximize').disabled")
                assert await page.locator(".resize-n").is_visible()
                await page.locator("#window-minimize").click()
                await page.wait_for_function("!document.getElementById('window-minimize').disabled")
                await page.locator(".resize-se").dispatch_event("mousedown", {"button": 0})
                await page.wait_for_function("!document.getElementById('window-minimize').disabled")
                await page.locator("#window-close").click()
                assert actions == [("window_toggle_maximize", {}), ("window_toggle_maximize", {}),
                                   ("window_minimize", {}), ("window_resize", {"edge": "se"}), ("window_close", {})]
                assert await page.evaluate("window.RainUI.getState().snapshot.state.phase") == "running"
                await page.emulate_media(reduced_motion="reduce")
                seconds = await page.locator("#window-minimize").evaluate("el=>parseFloat(getComputedStyle(el).transitionDuration)")
                assert seconds < .02
            finally:
                await browser.close()
    asyncio.run(scenario())
