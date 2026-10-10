"""PPT 正常翻页：服务器已读页码决定前进，章节进度决定最终完成。

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

    async def tick(self):
        now = time.monotonic()
        if now >= self.next_check:
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
        if not state['visible']:
            await self.page.bring_to_front()
            if now - self.last_change > self.config['video_stall_seconds']:
                raise RetryableError('PPT 页面持续处于隐藏状态，阅读记录未更新')
            return {'phase': '恢复 PPT 页面可见状态', 'ready': False}
        target = missing[0]
        if state['index'] != target:
            if now - self.last_change > self.config['video_stall_seconds']:
                raise RetryableError('PPT 翻页无响应，需要重新加载')
            # 只穿过已读页到下一未读页，每个 tick 验证真实活动页发生变化。
            await viewer.press('ArrowDown' if state['index'] < target else 'ArrowUp')
            return {'phase': f'定位未读页 {target+1}/{total}', 'ready': False}
        if state['hasMedia']:
            raise AdaptationRequired('该 PPT 页含音视频，请在网页完成嵌入媒体后重新扫描')
        if not state['loaded']:
            if now - self.last_change > self.config['video_stall_seconds']:
                raise RetryableError('PPT 图片加载失败，需要重新加载')
            return {'phase': f'加载 PPT 第 {target+1}/{total} 页', 'ready': False}
        if now - self.last_change > max(self.config['confirmation_timeout_seconds'], self.config['video_stall_seconds']):
            raise RetryableError('PPT 当前页服务器记录长期未更新，请检查连接或课程截止时间')
        return {'phase': f'阅读 PPT 第 {target+1}/{total} 页 · 服务器已读 {len(self.read_pages)}/{total}',
                'ready': False, 'pages_read': len(self.read_pages), 'pages_total': total,
                'current_page': target+1}
