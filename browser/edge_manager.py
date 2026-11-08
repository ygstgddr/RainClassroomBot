import asyncio
from pathlib import Path
import re
from urllib.parse import urlsplit
from playwright.async_api import ElementHandle, async_playwright
from core.errors import AdaptationRequired, Interrupted, LoginRequired
from browser.assessment_route import EXAM_ORIGIN, assessment_origin
from browser.visibility import visible_security_challenge


VUE_ASSESSMENT_ENTRY = r"""({classroom, leafId, leafType, title}) => {
    const normalize = value => String(value || '').replace(/\s+/g, ' ').trim();
    const matches = [];
    for (const row of document.querySelectorAll('.leaf-detail')) {
        if (row.closest('.is-custom-leaf') || !row.getClientRects().length ||
            getComputedStyle(row).visibility !== 'visible') continue;
        // The assessment row is rendered by leaf-detail through ElTooltip.
        const tooltip = row.__vue__;
        const component = tooltip?.$options?.name === 'ElTooltip' ? tooltip.$parent : tooltip;
        const name = component?.$options?.name || component?.$options?._componentTag;
        if (name !== 'leaf-detail' || component.$el !== row) continue;
        const props = component.$props;
        const leaf = props?.leafData;
        if (String(props?.classroom_id) !== classroom || String(leaf?.id) !== leafId ||
            leaf?.leaf_type !== leafType) continue;
        const labels = row.querySelectorAll('.leaf-title .title');
        if (labels.length === 1 && normalize(labels[0].textContent) === normalize(title)) matches.push(labels[0]);
    }
    return matches.length === 1 ? matches[0] : null;
}"""


class EdgeManager:
    def __init__(self, config):
        self.config = config
        self.playwright = self.context = self.page = None
        self.embedded = bool(config.get('embedded_endpoint'))
        self.cdp_browser = None

    async def start(self, headed=False):
        if self.embedded:
            await self._attach_embedded()
            return
        Path(self.config['profile_dir']).mkdir(parents=True, exist_ok=True)
        self.playwright = await async_playwright().start()
        self.context = await self.playwright.chromium.launch_persistent_context(
            self.config['profile_dir'], channel='msedge',
            headless=False if headed else self.config['headless'],
            # 仅静音本助手专用 Edge 进程；新标签、iframe 和恢复播放均适用。
            # 不修改 Windows 音量，也不影响用户自行打开的 Edge。
            args=['--mute-audio'],
            viewport={'width': 1280, 'height': 840},
            timeout=self.config['navigation_timeout_seconds'] * 1000)
        self.context.set_default_timeout(8000)
        self.context.set_default_navigation_timeout(self.config['navigation_timeout_seconds'] * 1000)
        self.page = self.context.pages[0] if self.context.pages else await self.context.new_page()
        # 恢复旧标签可能产生多个播放页面，只保留本助手专用 profile 的第一个标签。
        for extra in self.context.pages[1:]:
            await extra.close()
        await self.page.goto(self.config['base_url'] + self.config['entry_path'], wait_until='domcontentloaded')

    async def _attach_embedded(self):
        self.playwright = await async_playwright().start()
        endpoint = self.config['embedded_endpoint']
        for attempt in range(12):
            try:
                self.cdp_browser = await self.playwright.chromium.connect_over_cdp(endpoint, timeout=1800)
                break
            except Exception:
                if attempt == 11:
                    raise
                await asyncio.sleep(.4)
        contexts = self.cdp_browser.contexts
        if len(contexts) != 1 or len(contexts[0].pages) != 1:
            raise RuntimeError('内置浏览器必须包含唯一的工作页面')
        self.context, self.page = contexts[0], contexts[0].pages[0]
        self.context.set_default_timeout(8000)
        self.context.set_default_navigation_timeout(self.config['navigation_timeout_seconds'] * 1000)
        if self.page.url in ('', 'about:blank'):
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

    async def media_snapshot(self):
        """Bounded read-only probe, including video players inside page frames."""
        async def sample(frame):
            try:
                return await asyncio.wait_for(frame.evaluate('''() => {
                    const videos = [...document.querySelectorAll('video')];
                    const visible = videos.filter(v => v.getBoundingClientRect().width > 0 &&
                        v.getBoundingClientRect().height > 0 && getComputedStyle(v).visibility === 'visible');
                    const v = visible.find(x => !x.paused && !x.ended) || visible[0];
                    if (!v) return null;
                    return {state: v.error ? '播放错误' : v.ended ? '播放结束' : v.paused ? '已暂停' :
                        v.readyState < 3 ? '缓冲中' : '播放中', time: Math.round(v.currentTime * 10) / 10,
                        duration: Number.isFinite(v.duration) ? v.duration : null, rate: v.playbackRate};
                }'''), 2)
            except Exception:
                return None

        frames = self.page.frames[:24]
        results = [item for item in await asyncio.gather(*(sample(frame) for frame in frames)) if item]
        return next((item for item in results if item['state'] in ('播放中', '缓冲中')), results[0] if results else {})

    async def close(self):
        try:
            if self.embedded:
                if self.cdp_browser and self.cdp_browser.is_connected():
                    await asyncio.wait_for(self.cdp_browser.close(), 5)
            elif self.context:
                await asyncio.wait_for(self.context.close(), 12)
        finally:
            if self.playwright:
                await asyncio.wait_for(self.playwright.stop(), 8)
            self.context = self.page = self.playwright = None
            self.cdp_browser = None

    async def task_entry(self, task, *, require_id=False):
        """优先使用网站叶子节点 ID，避免章节/图文/视频的同名歧义。

        实站节点为 custom-<section_id>_<leaf_id>。只读定位后正常点击；
        旧页面没有这种节点时，仍要求所有 frame 中恰好一个可见同名入口。
        """
        leaf_id = str(task.get('leaf_id', ''))
        identified, named = [], []
        id_present = False
        base = urlsplit(self.config.get('base_url', ''))
        for frame in self.page.frames:
            if await visible_security_challenge(frame):
                raise LoginRequired('网站要求安全验证，请在 Edge 完成滑块验证后重新扫描并继续')
            current = frame
            while current.parent_frame:
                if not await (await current.frame_element()).is_visible():
                    break
                current = current.parent_frame
            if current.parent_frame:
                continue  # 隐藏的预加载 iframe 不参与任务入口匹配。
            if leaf_id.isascii() and leaf_id.isdigit():
                rows = frame.locator(f'.is-custom-leaf[id^="custom-"][id$="_{leaf_id}"]')
                for row in await rows.all():
                    id_present = True
                    if not await row.is_visible():
                        # Element UI 的已渲染折叠节点：只展开该任务的祖先。
                        parents = row.locator('xpath=ancestor::*[@role="treeitem" and @aria-expanded="false"]')
                        for parent in await parents.all():
                            toggle = parent.locator(':scope > .el-tree-node__content > .el-tree-node__expand-icon')
                            if await toggle.count() == 1 and await toggle.is_visible():
                                await toggle.click(timeout=3000)
                    if await row.is_visible() and await row.get_by_text(task['title'], exact=True).count() == 1:
                        # 整行右侧属于知识点容器；点击标题才能触发叶子任务
                        # 自己的 handler（网站在 .leaf-detail 上阻止事件冒泡）。
                        identified.append(row.get_by_text(task['title'], exact=True))
            if require_id and task.get('leaf_type') in (5, 6, 9) and base.netloc:
                frame_url = urlsplit(frame.url)
                if (frame_url.scheme, frame_url.netloc) == (base.scheme, base.netloc):
                    handle = await frame.evaluate_handle(VUE_ASSESSMENT_ENTRY, {
                        'classroom': str(task['classroom_id']), 'leafId': leaf_id,
                        'leafType': task['leaf_type'], 'title': task['title'],
                    })
                    element = handle.as_element()
                    if element is not None and await element.is_visible():
                        identified.append(element)
                    else:
                        await handle.dispose()
            if not require_id:
                for item in await frame.get_by_text(task['title'], exact=True).all():
                    if await item.is_visible():
                        named.append(item)
        if id_present or require_id:
            if len(identified) == 1:
                return identified[0]
            for entry in identified:
                if isinstance(entry, ElementHandle):
                    await entry.dispose()
            return None
        return named[0] if len(named) == 1 else None

    async def locate_task(self, task, *, entry_allowed=None):
        """Open course content and locate the task without opening its attempt."""
        course_url = f"{self.config['base_url']}/v2/web/studentLog/{task['classroom_id']}?tab=content"
        current, target = urlsplit(self.page.url), urlsplit(course_url)
        if entry_allowed is not None and not entry_allowed():
            raise Interrupted()
        # 验证暂停后原页面仍在：复用它，避免重新导航又触发验证。
        if (current.scheme, current.netloc, current.path) != (target.scheme, target.netloc, target.path):
            await self.page.goto(course_url, wait_until='domcontentloaded')
        deadline = asyncio.get_running_loop().time() + self.config['navigation_timeout_seconds']
        content_selected = False
        while asyncio.get_running_loop().time() < deadline:
            if entry_allowed is not None and not entry_allowed():
                raise Interrupted()
            # 长江站忽略 tab=content，需正常点击“学习内容”才会显示章节 iframe。
            if not content_selected:
                tabs = self.page.locator('.rain-tabs__nav-item').filter(has_text='学习内容')
                visible_tabs = [t for t in await tabs.all() if await t.is_visible()]
                if len(visible_tabs) == 1:
                    tab = visible_tabs[0]
                    if 'rain-tabs__nav-item--active' not in (await tab.get_attribute('class') or '').split():
                        await tab.click(timeout=3000)
                    content_selected = True
            entry = (await self.task_entry(task, require_id=True) if entry_allowed is not None
                     else await self.task_entry(task))
            if entry is not None:
                return entry
            await asyncio.sleep(.5)
        raise AdaptationRequired('课程页未找到唯一可见的任务入口；已核对任务 ID 与名称，请检查章节折叠状态或页面变化')

    async def open_task(self, task, *, entry_allowed=None):
        if task['kind'] == 'slides' and task.get('_slides_path'):
            await self.page.goto(self.config['base_url'] + task['_slides_path'], wait_until='domcontentloaded')
            return self.page
        template = self.config.get(f"{task['kind']}_path_template") if entry_allowed is None else None
        if template:
            url = self.config['base_url'] + template.format(**task)
            await self.page.goto(url, wait_until='domcontentloaded')
            return self.page
        entry = await self.locate_task(task, entry_allowed=entry_allowed)
        before = list(self.context.pages)
        try:
            if entry_allowed is not None and not entry_allowed():
                raise Interrupted()
            await entry.click()
        finally:
            if isinstance(entry, ElementHandle):
                await entry.dispose()
        # 某些页面在新标签播放；短轮询用于发现页面，而非等待任务完成。
        for _ in range(6):
            pages = [p for p in self.context.pages if p not in before]
            if pages:
                if self.embedded:
                    raise AdaptationRequired('任务打开了独立弹窗，当前内置浏览器无法确认任务页面')
                old = self.page
                self.page = pages[-1]
                await self.page.wait_for_load_state('domcontentloaded')
                await old.close()
                break
            await asyncio.sleep(.25)
        return self.page

    async def start_assessment(self, task, course_url, allowed):
        """Start only a unique control on the selected assessment's landing page."""
        course_path = urlsplit(course_url).path
        start_name = re.compile(r'^(开始考试|开始测验|开始答题|开始作答|开始练习|进入考试|进入答题)$')
        deadline = asyncio.get_running_loop().time() + 12
        clicked = False
        while allowed() and asyncio.get_running_loop().time() < deadline:
            starts = []
            at_course = urlsplit(self.page.url).path in (course_path, f"/v2/web/studentLog/{task.get('classroom_id', '')}")
            top_origin = assessment_origin(self.page.url, self.config['base_url'])
            if top_origin is None:
                await asyncio.sleep(.2)
                continue
            for frame in self.page.frames:
                if assessment_origin(frame.url, self.config['base_url']) != top_origin:
                    continue
                current = frame
                while current.parent_frame and await (await current.frame_element()).is_visible():
                    current = current.parent_frame
                if current.parent_frame:
                    continue
                surface = frame.get_by_role('dialog') if at_course else frame
                titles = surface.get_by_text(task['title'], exact=True)
                has_title = any([await title.is_visible() for title in await titles.all()])
                if top_origin == EXAM_ORIGIN and not has_title and not clicked:
                    continue
                questions = surface.locator('[data-question-id], fieldset, .subject-item')
                for question in await questions.all():
                    if await question.is_visible() and await question.locator('input:not([type=hidden]), textarea').count():
                        return True
                if clicked or not has_title:
                    continue
                starts.extend([button for button in await surface.get_by_role('button', name=start_name).all()
                               if await button.is_visible() and await button.is_enabled()])
            if len(starts) == 1 and allowed():
                await starts[0].click(timeout=3000)
                clicked = True
            elif len(starts) > 1:
                return False
            await asyncio.sleep(.2)
        return False
