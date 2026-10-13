import asyncio
import time
from core.errors import Interrupted, RetryableError, AdaptationRequired
from handler.video import VideoHandler
from handler.article import ArticleHandler
from handler.slides import SlidesHandler
from core.task_types import AUTOMATED_KINDS


async def _preflight_status(task, adapter, store):
    """任务进入前的 fresh 查询同样响应暂停；不留下旧账号/旧任务请求。"""
    query = asyncio.create_task(adapter.status(task))
    try:
        while True:
            if store.get('desired') != 'run':
                raise Interrupted()
            if query.done():
                return query.result()
            await asyncio.wait({query}, timeout=.1)
    finally:
        query.cancel()
        await asyncio.gather(query, return_exceptions=True)


async def execute(task, adapter, browser, store, config):
    if store.get('desired') != 'run':
        raise Interrupted()
    if task['kind'] not in AUTOMATED_KINDS:
        raise AdaptationRequired('该任务类型不支持自动阅读，请在网页处理')
    # 每次重试和重启首先向服务器复查，防止重复播放已完成任务。
    done, progress, evidence = await _preflight_status(task, adapter, store)
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
    # 开始前已经 fresh 查询一次，避免打开页面后立即重复请求整课进度。
    next_check = time.monotonic() + config['poll_seconds']
    status_query = None
    query_started = None
    last_duration = task.get('duration')
    next_publish = 0
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
            # 单个受控查询与媒体检查并行：慢服务器不能阻塞暂停、恢复播放和卡顿检测。
            # 不并发多个进度请求；每次都核验账号并读取新服务器响应。
            if status_query is not None and status_query.done():
                done, progress, evidence = status_query.result()
                status_query = None
                store.update(task['key'], progress=progress)
                fresh_final_check = confirm_started is None or query_started >= confirm_started
                next_check = time.monotonic() + config['poll_seconds'] if fresh_final_check else 0
                if done and fresh_final_check:
                    return evidence
            if (status_query is None and now >= next_check
                    and (task['kind'] != 'slides' or confirm_started is not None)):
                query_started = time.monotonic()
                status_query = asyncio.create_task(adapter.status(task))
            media = await asyncio.wait_for(handler.tick(), config['navigation_timeout_seconds'])
            if media.get('duration') and media['duration'] > 0 and media['duration'] != last_duration:
                store.update(task['key'], duration=media['duration'])
                last_duration = media['duration']
            now = time.monotonic()
            # PPT 每 0.1 秒检查可见性/翻页，界面数据最多每 0.25 秒持久化一次。
            # 暂停控制仍每次循环读取，不延迟用户操作。
            if now >= next_publish or media['ready'] and confirm_started is None:
                store.set('current', {'key': task['key'], 'course': task['course'], 'title': task['title'],
                                      'server_progress': progress, **media})
                next_publish = now + .25
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
        if status_query is not None:
            # 暂停/切站/任务退出后不遗留属于旧页面的请求和未读取异常。
            status_query.cancel()
            await asyncio.gather(status_query, return_exceptions=True)
        if hasattr(handler, 'close'):
            handler.close()
        await browser.pause_media()
        if task['kind'] == 'slides' and not page.is_closed():
            # PPT 自带阅读计时；暂停/出错时离开阅读页，避免后台继续累计。
            try:
                await page.goto(config['base_url'] + config['entry_path'], wait_until='domcontentloaded')
            except Exception:
                pass  # 浏览器崩溃时交由守护重建；原异常仍需传出。
