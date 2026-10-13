"""独立工作进程；Playwright 全部操作归属于同一 asyncio 事件循环。"""
import asyncio
import time
from browser.edge_manager import EdgeManager
from browser.site_adapter import SiteAdapter
from core.errors import LoginRequired, Interrupted
from core.logging_setup import make_logger, record
from core.store import Store
from core.workflow import Workflow


async def worker_async(config):
    store = Store(config['root'])
    logger = make_logger(config['root'])
    store.recover()
    store.set('worker_busy', False)
    browser = EdgeManager(config)

    async def pulse():
        while True:
            store.set('heartbeat', time.time())
            await asyncio.sleep(1)

    async def probe():
        while True:
            try:
                if browser.page and not browser.page.is_closed():
                    await asyncio.wait_for(browser.page.evaluate('() => document.readyState'), 8)
                    store.set('page_heartbeat', time.time())
            except Exception:
                pass  # 导航时上下文被替换属正常情况；长时间失败由 Watchdog 处理。
            await asyncio.sleep(3)

    heartbeat = asyncio.create_task(pulse())
    health = asyncio.create_task(probe())
    try:
        store.set('worker_status', '正在启动 Edge')
        headed = store.get('force_headed', False)
        await browser.start(headed=headed)
        adapter = SiteAdapter(browser, config)
        flow = Workflow(adapter, browser, store, config, logger)
        await browser.pause_media()
        previous = None
        while store.get('desired') != 'shutdown':
            desired = store.get('desired', 'idle')
            if desired in ('idle', 'paused'):
                flow.clear_login()
                if desired != previous:
                    phase = store.get('workflow', {}).get('phase')
                    if phase not in ('ready', 'paused', 'error', 'account_changed'):
                        flow.show('paused' if desired == 'paused' else 'ready',
                                  '已暂停，点击继续即可恢复' if desired == 'paused' else '选好课程后即可开始')
                previous = desired
                await asyncio.sleep(.5)
                continue
            previous = desired
            try:
                store.set('worker_busy', True)
                if desired == 'login':
                    await flow.poll_login()
                    # 每三秒探测登录；间隔内每半秒检查暂停/关闭，返回请求也再次检查。
                    store.set('worker_busy', False)
                    for _ in range(6):
                        if store.get('desired') != 'login':
                            break
                        await asyncio.sleep(.5)
                elif desired in ('catalog', 'scan', 'run'):
                    await flow.perform(desired)
                else:
                    flow.transition(desired, 'idle')
            except Interrupted:
                await browser.pause_media()
            except LoginRequired as exc:
                requested = flow.request_login(desired, str(exc))
                await browser.pause_media()
                if requested and config['headless'] and not headed:
                    store.set('force_headed', True)
                    # 退出后守护进程会以可见窗口重新打开；不在无头模式无限等待扫码。
                    return
            except Exception as exc:
                from core.errors import AdaptationRequired, RetryableError
                if isinstance(exc, AdaptationRequired):
                    if flow.transition(desired, 'paused'):
                        flow.show('error', str(exc))
                        record(store, logger, '需要适配', str(exc))
                elif isinstance(exc, RetryableError):
                    flow.show('error', '网络暂不可用，正在等待恢复…', desired)
                    record(store, logger, '网络重试', str(exc))
                    for _ in range(20):
                        if store.get('desired') != desired:
                            break
                        await asyncio.sleep(.5)
                else:
                    raise
            finally:
                store.set('worker_busy', False)
    finally:
        heartbeat.cancel()
        health.cancel()
        await asyncio.gather(heartbeat, health, return_exceptions=True)
        await browser.close()


def worker_entry(config):
    try:
        asyncio.run(worker_async(config))
    except Exception as exc:
        store = Store(config['root'])
        record(store, make_logger(config['root']), '工作进程退出', f'{type(exc).__name__}，由守护进程恢复')
        raise SystemExit(1)
