import asyncio
from pathlib import Path
from urllib.parse import urlsplit
from playwright.async_api import async_playwright
from core.errors import AdaptationRequired, LoginRequired
from browser.visibility import visible_security_challenge


class EdgeManager:
    def __init__(self, config):
        self.config = config
        self.playwright = self.context = self.page = None

    async def start(self, headed=False):
        Path(self.config['profile_dir']).mkdir(parents=True, exist_ok=True)
        self.playwright = await async_playwright().start()
        self.context = await self.playwright.chromium.launch_persistent_context(
            self.config['profile_dir'], channel='msedge',
            headless=False if headed else self.config['headless'],
            viewport={'width': 1280, 'height': 840},
            timeout=self.config['navigation_timeout_seconds'] * 1000)
        self.context.set_default_timeout(8000)
        self.context.set_default_navigation_timeout(self.config['navigation_timeout_seconds'] * 1000)
        self.page = self.context.pages[0] if self.context.pages else await self.context.new_page()
        # 恢复旧标签可能产生多个播放页面，只保留本助手专用 profile 的第一个标签。
        for extra in self.context.pages[1:]:
            await extra.close()
        await self.page.goto(self.config['base_url'] + self.config['entry_path'], wait_until='domcontentloaded')

    async def pause_media(self):
        if not self.context:
            return
        for page in self.context.pages:
            for frame in page.frames:
                try:
                    await asyncio.wait_for(frame.evaluate("() => document.querySelectorAll('video,audio').forEach(v => v.pause())"), 3)
                except Exception:
                    pass

    async def close(self):
        try:
            if self.context:
                await asyncio.wait_for(self.context.close(), 12)
        finally:
            if self.playwright:
                await asyncio.wait_for(self.playwright.stop(), 8)
            self.context = self.page = self.playwright = None

    async def open_task(self, task):
        if task['kind'] == 'slides' and task.get('_slides_path'):
            await self.page.goto(self.config['base_url'] + task['_slides_path'], wait_until='domcontentloaded')
            return self.page
        template = self.config.get(f"{task['kind']}_path_template")
        if template:
            url = self.config['base_url'] + template.format(**task)
            await self.page.goto(url, wait_until='domcontentloaded')
            return self.page
        # 不假设视频 URL：从该课程页定位真实可见的任务。
        course_url = f"{self.config['base_url']}/v2/web/studentLog/{task['classroom_id']}?tab=content"
        current, target = urlsplit(self.page.url), urlsplit(course_url)
        # 验证暂停后原页面仍在：复用它，避免重新导航又触发验证。
        if (current.scheme, current.netloc, current.path) != (target.scheme, target.netloc, target.path):
            await self.page.goto(course_url, wait_until='domcontentloaded')
        deadline = asyncio.get_running_loop().time() + self.config['navigation_timeout_seconds']
        content_selected = False
        while asyncio.get_running_loop().time() < deadline:
            # 长江站忽略 tab=content，需正常点击“学习内容”才会显示章节 iframe。
            if not content_selected:
                tabs = self.page.locator('.rain-tabs__nav-item').filter(has_text='学习内容')
                visible_tabs = [t for t in await tabs.all() if await t.is_visible()]
                if len(visible_tabs) == 1:
                    tab = visible_tabs[0]
                    if 'rain-tabs__nav-item--active' not in (await tab.get_attribute('class') or '').split():
                        await tab.click(timeout=3000)
                    content_selected = True
            for frame in self.page.frames:
                if await visible_security_challenge(frame):
                    raise LoginRequired('网站要求安全验证，请在 Edge 完成滑块验证后重新扫描并继续')
                candidate = frame.get_by_text(task['title'], exact=True)
                visible = []
                for item in await candidate.all():
                    if await item.is_visible():
                        visible.append(item)
                if len(visible) == 1:
                    before = list(self.context.pages)
                    await visible[0].click()
                    # 某些页面在新标签播放；短轮询用于发现页面，而非等待任务完成。
                    for _ in range(6):
                        pages = [p for p in self.context.pages if p not in before]
                        if pages:
                            old = self.page
                            self.page = pages[-1]
                            await self.page.wait_for_load_state('domcontentloaded')
                            await old.close()
                            break
                        await asyncio.sleep(.25)
                    return self.page
            await asyncio.sleep(.5)
        raise AdaptationRequired('课程页未找到唯一可见的任务名称；请检查章节折叠状态或配置已核验的路径模板')
