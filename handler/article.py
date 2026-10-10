import time
from core.errors import AdaptationRequired


class ArticleHandler:
    def __init__(self, page, config):
        self.page, self.config = page, config
        self.last_step = 0
        self.next_step = time.monotonic()
        self.bottom_passes = 0
        self.ready = False
        self.started = time.monotonic()
        self.clicked = False

    async def tick(self):
        target = None
        for frame in self.page.frames:
            for selector in self.config['article_selectors']:
                loc = frame.locator(selector)
                if await loc.count() == 1 and await loc.is_visible():
                    target = loc
                    break
            if target:
                break
        if target is None:
            if time.monotonic() - self.started > self.config['navigation_timeout_seconds']:
                raise AdaptationRequired('未找到图文正文，请更新 article_selectors')
            return {'phase': '等待图文正文', 'ready': False}
        geometry = await target.evaluate('''el => {
            let s=el;
            while(s && !(s.scrollHeight>s.clientHeight+5 && /(auto|scroll)/.test(getComputedStyle(s).overflowY))) s=s.parentElement;
            s=s||document.scrollingElement;
            return {height:s.clientHeight, top:s.scrollTop, total:s.scrollHeight,
                chars:el.innerText.length, loaded:Array.from(el.querySelectorAll('img')).every(i=>i.complete&&i.naturalWidth>0)};
        }''')
        if not geometry['loaded']:
            if time.monotonic() - self.started > self.config['video_stall_seconds']:
                raise AdaptationRequired('图文中的图片未成功加载')
            return {'phase': '等待图片加载', 'ready': False}
        now = time.monotonic()
        if now >= self.next_step and not self.ready:
            at_bottom = geometry['top'] + geometry['height'] >= geometry['total'] - 3
            self.bottom_passes = self.bottom_passes + 1 if at_bottom else 0
            self.ready = self.bottom_passes >= 2
            # 按正文长度和视口占比安排阅读间隔；间隔绝不作为完成证据。
            visible_chars = geometry['chars'] * min(1, geometry['height'] / max(1, geometry['total']))
            self.next_step = now + max(2, min(45, visible_chars / self.config['reading_characters_per_minute'] * 60))
            if not at_bottom:
                await target.evaluate('''el => {
                    let s=el;
                    while(s && !(s.scrollHeight>s.clientHeight+5 && /(auto|scroll)/.test(getComputedStyle(s).overflowY))) s=s.parentElement;
                    s=s||document.scrollingElement; s.scrollBy(0, Math.max(100,s.clientHeight*0.7));
                }''')
        if self.ready and not self.clicked:
            for frame in self.page.frames:
                for label in self.config['article_finish_labels']:
                    button = frame.get_by_role('button', name=label, exact=True)
                    if await button.count() == 1 and await button.is_visible() and await button.is_enabled():
                        await button.click()
                        self.clicked = True
                        break
                if self.clicked:
                    break
        return {'phase': '等待服务器确认' if self.ready else '阅读滚动中', 'ready': self.ready,
                'reading_progress': min(1, (geometry['top'] + geometry['height']) / max(1, geometry['total']))}
