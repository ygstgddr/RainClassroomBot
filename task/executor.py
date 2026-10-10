import asyncio
import time
from core.errors import Interrupted, RetryableError, AdaptationRequired
from handler.video import VideoHandler
from handler.article import ArticleHandler
from handler.slides import SlidesHandler
from core.task_types import AUTOMATED_KINDS


async def execute(task, adapter, browser, store, config):
    if store.get('desired') != 'run':
        raise Interrupted()
    if task['kind'] not in AUTOMATED_KINDS:
        raise AdaptationRequired('该任务类型不支持自动阅读，请在网页处理')
    # 每次重试和重启首先向服务器复查，防止重复播放已完成任务。
    done, progress, evidence = await adapter.status(task)
    if done:
        return evidence
    info = await adapter.prepare_slides(task) if task['kind'] == 'slides' else None
    if store.get('desired') != 'run':
        raise Interrupted()
    entry_task = dict(task, _slides_path=info['path']) if info else task
    page = await browser.open_task(entry_task)
    handler = SlidesHandler(page, config, adapter, info) if info else VideoHandler(page, config) if task['kind'] == 'video' else ArticleHandler(page, config)
    started = time.monotonic()
    confirm_started = None
    next_check = 0
    try:
        while True:
            if store.get('desired') != 'run':
                await browser.pause_media()
                raise Interrupted()
            now = time.monotonic()
            if now - started > config['task_timeout_seconds']:
                raise RetryableError('任务超过最长运行上限，仍未确认完成')
            if page.is_closed():
                raise RetryableError('任务页面被关闭')
            if now >= next_check and (task['kind'] != 'slides' or confirm_started is not None):
                done, progress, evidence = await adapter.status(task)
                store.update(task['key'], progress=progress)
                next_check = time.monotonic() + config['poll_seconds']
                if done:
                    return evidence
            media = await asyncio.wait_for(handler.tick(), config['navigation_timeout_seconds'])
            if media.get('duration') and media['duration'] > 0:
                store.update(task['key'], duration=media['duration'])
            store.set('current', {'key': task['key'], 'course': task['course'], 'title': task['title'],
                                  'server_progress': progress, **media})
            if media['ready']:
                if confirm_started is None:
                    # 本轮操作结束后立即复查一次，不额外等满一次普通轮询间隔。
                    next_check = 0
                confirm_started = confirm_started or now
            if confirm_started is not None and now - confirm_started > config['confirmation_timeout_seconds']:
                raise RetryableError('页面操作已结束，但服务器未确认完成')
            # 仅为状态采样频率；不以经过时间推断任务完成。
            await asyncio.sleep(min(.1 if task['kind'] == 'slides' else .5, config['poll_seconds']))
    finally:
        if hasattr(handler, 'close'):
            handler.close()
        await browser.pause_media()
        if task['kind'] == 'slides' and not page.is_closed():
            # PPT 自带阅读计时；暂停/出错时离开阅读页，避免后台继续累计。
            try:
                await page.goto(config['base_url'] + config['entry_path'], wait_until='domcontentloaded')
            except Exception:
                pass  # 浏览器崩溃时交由守护重建；原异常仍需传出。
