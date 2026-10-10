"""通过播放器公开菜单选择最高倍速；只读 playbackRate 验证结果。"""
import math
import re
import time

from playwright.async_api import Error as PlaywrightError


class NativeVideoSpeed:
    # 新学习中心和旧版 XT 播放器。所有定位均限制在目标 video 的最近播放器内。
    ADAPTERS = (
        (".//*[contains(concat(' ',normalize-space(@class),' '),' rate-btn-box ')]",
         '.rate-btn-box', '.rate-config-item', None),
        ('.//xt-speedbutton', 'xt-speedbutton', 'li[data-speed]', 'data-speed'),
    )

    def __init__(self, enabled=True, retry_seconds=5):
        self.enabled = enabled
        self.retry_seconds = retry_seconds
        self.next_probe = 0
        self.maximum = None
        self.note = '等待倍速菜单'
        self.video_handle = None
        self.source = None

    @staticmethod
    def parse_rate(text):
        match = re.fullmatch(r'\s*(\d+(?:\.\d+)?)\s*(?:[xX×]|倍)?\s*', text or '')
        rate = float(match[1]) if match else None
        return rate if rate and math.isfinite(rate) and rate > 0 else None

    async def ensure(self, video):
        """每次读取实际速率，限频重试菜单；失败不会阻止正常播放。"""
        actual = await video.evaluate('v => v.playbackRate')
        if not self.enabled:
            return dict(playback_rate=actual, max_playback_rate=None, speed_confirmed=False,
                        speed_note='保留播放器倍速')
        source = await video.evaluate('v => v.currentSrc || v.getAttribute("src") || ""')
        try:
            same = self.video_handle is not None and await video.evaluate('(v, old) => v === old', self.video_handle)
        except PlaywrightError:
            same = False
        if not same or source != self.source:
            if self.video_handle is not None:
                try:
                    await self.video_handle.dispose()
                except PlaywrightError:
                    pass
            self.video_handle = await video.element_handle()
            self.source = source
            self.maximum = None
            self.next_probe = 0
        # 已生效时只监测实际速率，不再每隔数秒移动鼠标展开菜单。
        if self.maximum is not None and abs(actual - self.maximum) < .001:
            return dict(playback_rate=actual, max_playback_rate=self.maximum,
                        speed_confirmed=True, speed_note='播放器最高倍速')
        if time.monotonic() >= self.next_probe:
            self.next_probe = time.monotonic() + self.retry_seconds
            self.maximum = None
            self.note = '未找到可用倍速菜单'
            try:
                await self._select(video, actual)
            except PlaywrightError:
                # 页面刷新、菜单消失或控件暂不可点击：下次探测重试，不输出含敏感网址的异常。
                self.note = '倍速设置暂未确认，将重试'
            actual = await video.evaluate('v => v.playbackRate')
        confirmed = self.maximum is not None and abs(actual - self.maximum) < .001
        return dict(playback_rate=actual, max_playback_rate=self.maximum,
                    speed_confirmed=confirmed,
                    speed_note='播放器最高倍速' if confirmed else self.note)

    async def _select(self, video, actual):
        for predicate, trigger_css, options_css, attribute in self.ADAPTERS:
            root = video.locator(f'xpath=ancestor::*[{predicate}][1]')
            if not await root.count():
                continue
            visible_videos = [v for v in await root.locator('video').all() if await v.is_visible()]
            if len(visible_videos) != 1:
                continue
            trigger = root.locator(trigger_css)
            if await trigger.count() != 1:
                continue
            # 新版播放器在 video 上覆盖画布/封面，悬停容器才能触发正常控件。
            await root.hover(timeout=1200)
            await trigger.hover(timeout=1200)
            options = trigger.locator(options_css)
            # 部分版本点击后才展开，先尝试鼠标悬停，避免把已展开菜单关闭。
            if not any([await item.is_visible() for item in await options.all()]):
                await trigger.click(timeout=1200)
            available = []
            for item in await options.all():
                if not await item.is_visible() or not await item.is_enabled():
                    continue
                disabled = await item.evaluate("e => !!e.closest('[aria-disabled=true], [disabled], .disabled, .is-disabled')")
                if disabled:
                    continue
                raw = await item.get_attribute(attribute) if attribute else await item.inner_text()
                rate = self.parse_rate(raw)
                if rate is not None:
                    available.append((rate, item))
            if not available:
                continue
            self.maximum, option = max(available, key=lambda pair: pair[0])
            self.note = '最高倍速暂未生效，将重试'
            if abs(actual - self.maximum) >= .001:
                if trigger_css == 'xt-speedbutton':
                    # XT 在 mouseover 中重置坐标，只有选项内部发生 mousemove
                    # 后才接受 click。直接从触发器跳到选项中心点击会被忽略。
                    box = await option.bounding_box()
                    if not box:
                        return
                    first = {'x': box['width'] * .35, 'y': box['height'] * .5}
                    last = {'x': box['width'] * .65, 'y': box['height'] * .5}
                    await option.hover(position=first, timeout=1200)
                    await option.hover(position=last, timeout=1200)
                    await option.click(position=last, timeout=1200)
                else:
                    await option.click(timeout=1200)
            await root.hover(timeout=1200)
            return
