"""PPT 分批阅读：正常停留和翻页，服务器记录决定最终完成。

只读 DOM/GET 状态，翻页使用网站支持的键盘操作。不调用 Vue、Swiper 内部方法，
不发送学习 WebSocket 消息，不伪造停留时长。恢复后自动跳过服务器已读页。
"""
import time
from core.errors import AdaptationRequired, RetryableError


class SlidesHandler:
    def __init__(self, page, config, adapter, info):
        self.page, self.config, self.adapter, self.info = page, config, adapter, info
        self.read_pages = set()
        self.next_check = 0
        self.last_change = time.monotonic()
        self.last_index = None
        self.last_read = set()
        # visited 只表示本轮页面操作完成，绝不能用作服务器完成证据。
        self.visited = set()
        self.reading_since = None
        self.waiting_since = None
        self.round = 1

    async def tick(self):
        now = time.monotonic()
        # 首次取得已读页用于恢复；浏览期间不让慢 HTTP 阻塞翻页，轮末再批量复查。
        if now >= self.next_check and (self.next_check == 0 or self.waiting_since is not None):
            self.read_pages = await self.adapter.slide_depth(self.info)
            self.next_check = time.monotonic() + self.config['poll_seconds']
            if self.read_pages != self.last_read:
                self.last_change = now
                self.last_read = self.read_pages.copy()
        total = self.info['total']
        missing = sorted(set(range(total)) - self.read_pages)
        if not missing:
            return {'phase': f'PPT 已读 {total}/{total} 页，复查章节完成状态', 'ready': True,
                    'pages_read': total, 'pages_total': total}
        remaining = sorted(set(missing) - self.visited)
        if not remaining:
            if await self.page.evaluate("document.visibilityState !== 'visible'"):
                await self.page.bring_to_front()
            # 网站可能批量、延迟保存；不因单页查询未返回而卡住下一页。
            self.waiting_since = self.waiting_since or now
            grace = min(35, self.config['confirmation_timeout_seconds'] / 2)
            if now - self.waiting_since >= grace and self.round < self.config['max_task_attempts']:
                self.visited.intersection_update(self.read_pages)
                remaining = missing
                self.round += 1
                self.waiting_since = None
                self.reading_since = None
            else:
                return {'phase': f'PPT 本轮浏览结束 · 等待服务器确认 {len(self.read_pages)}/{total} 页',
                        'ready': True, 'pages_read': len(self.read_pages), 'pages_total': total,
                        'pages_visited': len(self.visited), 'reading_round': self.round}
        viewers = []
        for frame in self.page.frames:
            for viewer in await frame.locator('.pptSwiper').all():
                if await viewer.is_visible():
                    viewers.append(viewer)
        if len(viewers) > 1:
            raise AdaptationRequired('存在多个 PPT 阅读器，无法确定目标')
        if not viewers:
            if now - self.last_change > self.config['navigation_timeout_seconds']:
                raise RetryableError('PPT 阅读器加载失败，需重建页面')
            return {'phase': '等待 PPT 阅读器', 'ready': False}
        viewer = viewers[0]
        state = await viewer.evaluate('''el => {
            const slides = Array.from(el.querySelector(':scope > .swiper-wrapper')?.children || [])
                .filter(s=>s.classList.contains('swiper-slide'));
            const index = slides.findIndex(s=>s.classList.contains('swiper-slide-active'));
            const active = slides[index];
            const imgs = active ? Array.from(active.querySelectorAll('img')).filter(i=>i.getAttribute('src') && getComputedStyle(i).display!=='none') : [];
            return {index, total:slides.length,
                loaded:imgs.length>0 && imgs.every(i=>i.complete && i.naturalWidth>0),
                hasMedia:!!active?.querySelector('video,audio'),
                visible:document.visibilityState==='visible'};
        }''')
        if state['total'] != total or state['index'] < 0:
            raise AdaptationRequired('PPT 阅读器页数与服务器目录不一致')
        if state['index'] != self.last_index:
            self.last_index = state['index']
            self.last_change = now
            self.reading_since = None
        if not state['visible']:
            self.reading_since = None
            await self.page.bring_to_front()
            if now - self.last_change > self.config['video_stall_seconds']:
                raise RetryableError('PPT 页面持续处于隐藏状态，阅读记录未更新')
            return {'phase': '恢复 PPT 页面可见状态', 'ready': False}
        target = remaining[0]
        if state['index'] != target:
            if now - self.last_change > self.config['video_stall_seconds']:
                raise RetryableError('PPT 翻页无响应，需要重新加载')
            self.reading_since = None
            # 只用网站键盘交互，下一次 tick 核验活动页；不调用内部翻页方法。
            await viewer.press('ArrowDown' if state['index'] < target else 'ArrowUp')
            return {'phase': f'定位未读页 {target+1}/{total}', 'ready': False, 'reading_round': self.round}
        if state['hasMedia']:
            raise AdaptationRequired('该 PPT 页含音视频，请在网页完成嵌入媒体后重新扫描')
        if not state['loaded']:
            self.reading_since = None
            if now - self.last_change > self.config['video_stall_seconds']:
                raise RetryableError('PPT 图片加载失败，需要重新加载')
            return {'phase': f'加载 PPT 第 {target+1}/{total} 页', 'ready': False, 'reading_round': self.round}
        # 只在当前页可见、图片加载完成后累计实际停留时间。
        now = time.monotonic()
        if self.reading_since is None:
            self.reading_since = now
        elapsed = now - self.reading_since
        dwell = self.config.get('slides_page_seconds', 5)
        if elapsed >= dwell:
            self.visited.add(target)
            self.reading_since = None
            return {'phase': f'PPT 第 {target+1}/{total} 页已浏览，继续下一页', 'ready': False,
                    'pages_read': len(self.read_pages), 'pages_total': total,
                    'pages_visited': len(self.visited), 'current_page': target+1, 'reading_round': self.round}
        return {'phase': f'阅读 PPT 第 {target+1}/{total} 页 · {elapsed:.1f}/{dwell:g} 秒 · 服务器已读 {len(self.read_pages)}/{total}',
                'ready': False, 'pages_read': len(self.read_pages), 'pages_total': total,
                'current_page': target+1, 'pages_visited': len(self.visited), 'reading_round': self.round}
