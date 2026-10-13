"""Edge UI checks for readable courses and explicitly targeted text output.

Fictional bridge snapshots exercise consent/focus behavior without typing into
the user's website or retaining the source text outside this test.
"""
import asyncio
from copy import deepcopy

from playwright.async_api import async_playwright

from gui.web_assets import load_document


def display_snapshot():
    return {
        "version": "0.7.0", "account": {"name": "演示同学", "logged_in": True},
        "site": {"label": "演示站点", "url": "https://demo.invalid/web"},
        "state": {"phase": "idle", "busy": False, "selection_locked": False,
                  "primary": {"action": "start", "label": "开始学习", "enabled": True}},
        "courses": [{"id": "demo-1", "name": "计算机基础与 Python 程序设计及数据分析综合实践课程",
                     "start_date": "2026-09-01", "end_date": "2027-01-10", "status": "进行中", "selected": True}],
        "typing": {"phase": "idle", "available": True, "learning_busy": False, "sent": 0, "total": 0, "speed": 20},
    }


def target(token="first", multiline=True):
    return {"token": token, "label": "作业正文", "url": "https://demo.invalid/homework",
            "frame": "主页面", "tag": "textarea" if multiline else "input",
            "type": "textarea" if multiline else "text", "multiline": multiline, "characters": 5}


async def open_page(browser, snapshot, actions):
    page = await browser.new_page(viewport={"width": 1280, "height": 850})

    def command(name, payload):
        actions.append((name, payload))
        if name == "typing_start":
            snapshot["typing"].update(phase="running", total=len(payload["text"]), sent=0)
        elif name == "typing_stop":
            snapshot["typing"].update(phase="stopped", target=None)
        elif name == "typing_cancel":
            snapshot["typing"].update(phase="idle", target=None)
        return {"ok": True}

    await page.expose_function("testSnapshot", lambda: deepcopy(snapshot))
    await page.expose_function("testAction", command)
    bootstrap = '<script>window.storageWrites=0;Storage.prototype.setItem=function(){window.storageWrites+=1};window.pywebview={api:{snapshot:()=>window.testSnapshot(),action:(n,p)=>window.testAction(n,p)}};</script>'
    await page.set_content(load_document().replace("<head>", "<head>" + bootstrap, 1))
    await page.wait_for_function("window.RainUI && document.querySelectorAll('.course-card').length===1")
    return page


def test_courses_are_readable_and_complete_at_compact_width():
    async def scenario():
        async with async_playwright() as engine:
            browser = await engine.chromium.launch(channel="msedge", headless=True)
            try:
                page = await open_page(browser, display_snapshot(), [])
                for width in (1540, 1280, 900):
                    await page.set_viewport_size({"width": width, "height": 660})
                    styles = await page.locator(".course-card").evaluate("""el=>({
                        title:parseFloat(getComputedStyle(el.querySelector('h3')).fontSize),
                        dates:parseFloat(getComputedStyle(el.querySelector('.course-date')).fontSize),
                        state:parseFloat(getComputedStyle(el.querySelector('.pill')).fontSize),
                        clipped:el.querySelector('h3').scrollHeight>el.querySelector('h3').clientHeight,
                        overflow:document.documentElement.scrollWidth>innerWidth})""")
                    assert styles["title"] >= 18 and styles["dates"] >= 12 and styles["state"] >= 12
                    assert not styles["clipped"] and not styles["overflow"]
            finally:
                await browser.close()
    asyncio.run(scenario())


def test_only_confirmed_target_can_receive_source_and_snapshots_keep_editor():
    async def scenario():
        snapshot, actions = display_snapshot(), []
        async with async_playwright() as engine:
            browser = await engine.chromium.launch(channel="msedge", headless=True)
            try:
                page = await open_page(browser, snapshot, actions)
                await page.locator('[data-page="tools"]').click()
                await page.locator("#page-tools").wait_for(state="visible")
                assert not await page.locator(".hero").is_visible()
                text = '<img src=x onerror="window.BAD_UI=true">\n这是一段自己的作业内容。'
                await page.locator("#typing-source").fill(text)
                assert await page.locator("#typing-start").is_disabled()
                assert not actions
                snapshot["typing"].update(phase="ready", target=target())
                await page.evaluate("data=>window.RainUI.applySnapshot(data)", snapshot)
                assert await page.locator("#typing-start").is_disabled()
                await page.locator("#typing-confirm").click()
                assert not await page.locator("#typing-start").is_disabled()
                await page.locator("#typing-source").focus()
                await page.evaluate('window.keptEditor=document.getElementById("typing-source")')
                snapshot["typing"].update(sent=1, total=30)
                await page.evaluate("data=>window.RainUI.applySnapshot(data)", snapshot)
                assert await page.locator("#typing-source").input_value() == text
                assert await page.evaluate('window.keptEditor===document.getElementById("typing-source") && document.activeElement===window.keptEditor')
                assert not await page.evaluate("!!window.BAD_UI")
                await page.locator("#typing-start").click()
                await page.wait_for_function("window.RainUI.getState().snapshot.typing.phase==='running'")
                assert actions == [("typing_start", {"token": "first", "text": text, "speed": 20, "confirmed": True})]
                assert await page.locator("#typing-source").get_attribute("readonly") is not None
                assert await page.locator("#primary-button").is_disabled()
                await page.locator("#typing-stop").click()
                await page.wait_for_function("window.RainUI.getState().snapshot.typing.phase==='stopped'")
                assert await page.locator("#typing-start").is_disabled()
                assert await page.locator("#typing-source").input_value() == text
                assert not await page.evaluate("window.storageWrites")
            finally:
                await browser.close()
    asyncio.run(scenario())


def test_target_changes_single_line_and_learning_lock_prevent_output():
    async def scenario():
        snapshot, actions = display_snapshot(), []
        async with async_playwright() as engine:
            browser = await engine.chromium.launch(channel="msedge", headless=True)
            try:
                page = await open_page(browser, snapshot, actions)
                await page.locator('[data-page="tools"]').click()
                await page.locator("#page-tools").wait_for(state="visible")
                await page.locator("#typing-source").fill("第一行\n第二行")
                snapshot["typing"].update(phase="ready", target=target(multiline=False))
                await page.evaluate("data=>window.RainUI.applySnapshot(data)", snapshot)
                await page.locator("#typing-confirm").click()
                assert await page.locator("#typing-multiline-notice").is_visible()
                assert await page.locator("#typing-start").is_disabled()
                await page.locator("#typing-source").fill("单行文字")
                assert not await page.locator("#typing-start").is_disabled()
                snapshot["typing"]["target"] = target(token="another", multiline=False)
                await page.evaluate("data=>window.RainUI.applySnapshot(data)", snapshot)
                assert await page.locator("#typing-start").is_disabled()
                await page.locator("#typing-confirm").click()
                snapshot["typing"]["learning_busy"] = True
                await page.evaluate("data=>window.RainUI.applySnapshot(data)", snapshot)
                assert await page.locator("#typing-busy-notice").is_visible()
                assert await page.locator("#typing-start").is_disabled()
                assert await page.locator("#typing-pick").is_disabled()
                await page.set_viewport_size({"width": 900, "height": 660})
                assert not await page.evaluate("document.documentElement.scrollWidth>innerWidth")
                assert not actions
            finally:
                await browser.close()
    asyncio.run(scenario())


def test_escape_closes_layers_before_cancelling_and_stopping_with_draft_intact():
    async def scenario():
        snapshot, actions = display_snapshot(), []
        async with async_playwright() as engine:
            browser = await engine.chromium.launch(channel="msedge", headless=True)
            try:
                page = await open_page(browser, snapshot, actions)
                await page.locator('[data-page="tools"]').click()
                await page.locator("#page-tools").wait_for(state="visible")
                draft = "自己的原文🙂"
                await page.locator("#typing-source").fill(draft)
                await page.keyboard.press("Escape")
                assert not actions and await page.locator("#typing-source").input_value() == draft
                # Count Unicode code points, as the backend does, rather than
                # incorrectly treating an emoji as two browser UTF-16 units.
                await page.locator("#typing-source").fill("🙂" * 50001)
                assert await page.locator("#typing-multiline-notice").is_visible()
                assert "50,001" in await page.locator("#typing-character-count").inner_text()
                await page.locator("#typing-source").fill(draft)
                snapshot["typing"].update(phase="selecting")
                await page.evaluate("data=>window.RainUI.applySnapshot(data)", snapshot)
                await page.locator("#site-button").click()
                await page.keyboard.press("Escape")
                assert not await page.locator("#settings-overlay").is_visible()
                assert not actions
                await page.keyboard.press("Escape")
                await page.wait_for_function("window.RainUI.getState().snapshot.typing.phase==='idle' && !window.RainUI.getState().pending")
                assert actions == [("typing_cancel", {})]
                snapshot["typing"].update(phase="running", target=target(), sent=2, total=10)
                await page.evaluate("data=>window.RainUI.applySnapshot(data)", snapshot)
                assert await page.locator("#typing-source").get_attribute("readonly") is not None
                snapshot["typing"]["phase"] = "paused"
                await page.evaluate("data=>window.RainUI.applySnapshot(data)", snapshot)
                assert await page.locator("#typing-source").get_attribute("readonly") is not None
                await page.keyboard.press("Escape")
                await page.wait_for_function("window.RainUI.getState().snapshot.typing.phase==='stopped'")
                assert actions[-1] == ("typing_stop", {})
                assert await page.locator("#typing-source").input_value() == draft
                assert await page.locator("#typing-source").get_attribute("readonly") is None
            finally:
                await browser.close()
    asyncio.run(scenario())
