import time
from core.errors import RetryableError, AdaptationRequired
from handler.video_speed import NativeVideoSpeed


class VideoHandler:
    def __init__(self, page, config):
        self.page, self.config = page, config
        self.last_time = -1
        self.last_movement = time.monotonic()
        self.ended = False
        self.network_errors = 0
        self.speed = NativeVideoSpeed(config.get('auto_max_video_speed', True))
        self.page.on('response', self.on_response)
        self.page.on('requestfailed', self.on_failure)

    def on_response(self, response):
        if response.request.resource_type == 'media' and response.status >= 400:
            self.network_errors += 1

    def on_failure(self, request):
        if request.resource_type == 'media':
            self.network_errors += 1

    def close(self):
        self.page.remove_listener('response', self.on_response)
        self.page.remove_listener('requestfailed', self.on_failure)

    async def tick(self):
        media = []
        for frame in self.page.frames:
            for video in await frame.locator('video').all():
                if await video.is_visible():
                    media.append(video)
        if len(media) > 1:
            raise AdaptationRequired('页面有多个可见视频，无法确认目标播放器')
        if not media:
            if time.monotonic() - self.last_movement > self.config['video_stall_seconds']:
                raise RetryableError('未加载出可见 video 元素，需要重新加载页面')
            return {'phase': '等待播放器', 'ready': False}
        video = media[0]
        state = await video.evaluate('''v => ({time:v.currentTime, duration:Number.isFinite(v.duration)?v.duration:null,
            paused:v.paused, ended:v.ended, readyState:v.readyState,
            networkState:v.networkState, error:v.error ? v.error.code : null})''')
        if state['error']:
            raise RetryableError(f"视频加载失败，媒体错误码 {state['error']}")
        if state['time'] > self.last_time + .1:
            self.last_time = state['time']
            self.last_movement = time.monotonic()
        self.ended = bool(state['ended'])
        if state['paused'] and not self.ended:
            # 正常播放；倍速只通过网站菜单设置，不改 currentTime 或学习上报。
            result = await video.evaluate("async v => { try { await v.play(); return true; } catch(e) { return false; } }")
            if not result:
                await video.click()
        if not self.ended and time.monotonic() - self.last_movement > self.config['video_stall_seconds']:
            raise RetryableError(f'视频进度停滞；媒体网络失败次数 {self.network_errors}')
        phase = '等待服务器确认' if self.ended else '播放中'
        if not self.ended and state['readyState'] >= 1:
            speed = await self.speed.ensure(video)
            state.update(speed)
            suffix = '（播放器最高）' if speed['speed_confirmed'] else ''
            phase += f" · {speed['playback_rate']:g}×{suffix}"
        state.update(phase=phase, ready=self.ended,
                     network_errors=self.network_errors)
        return state
