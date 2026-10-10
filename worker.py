"""独立工作进程；Playwright 全部操作归属于同一 asyncio 事件循环。"""
import asyncio
import time
from browser.edge_manager import EdgeManager
from browser.site_adapter import SiteAdapter
from core.errors import LoginRequired, Interrupted
from core.logging_setup import make_logger, record
from core.store import Store
from task.scanner import scan
from task.scheduler import run_queue


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
        await browser.start(headed=store.get('force_headed', False))
        adapter = SiteAdapter(browser, config)
        await browser.pause_media()
        while store.get('desired') != 'shutdown':
            desired = store.get('desired', 'idle')
            store.set('worker_status', {'idle': '就绪', 'paused': '已暂停', 'login': store.get('login_message', '请在 Edge 登录后点击“刷新课程时间”'),
                                        'catalog': '读取课程时间', 'scan': '扫描中', 'run': '执行中'}.get(desired, desired))
            if desired in ('idle', 'paused', 'login'):
                await asyncio.sleep(.5)
                continue
            try:
                store.set('worker_busy', True)
                if desired in ('scan', 'catalog'):
                    adapter.account_id = None  # 用户主动扫描允许识别新账号。
                    result = await scan(adapter, store, config, logger, catalog_only=desired == 'catalog')
                    if store.get('desired') == desired:
                        store.set('desired', 'paused' if result['status'] == 'failed' else 'idle')
                elif desired == 'run':
                    account = await adapter.account()
                    store.set('account', account)
                    if not store.scope_scanned(account['id']):
                        result = await scan(adapter, store, config, logger)
                        if result['status'] == 'failed':
                            store.set('desired', 'paused')
                            continue
                    await run_queue(adapter, browser, store, config, logger)
                else:
                    store.set('desired', 'idle')
            except Interrupted:
                await browser.pause_media()
            except LoginRequired as exc:
                store.set('login_message', str(exc))
                store.set('desired', 'login')
                store.set('force_headed', True)
                record(store, logger, '需要登录', str(exc))
                await browser.pause_media()
                if config['headless']:
                    # 退出后守护进程会以可见窗口重新打开；不在无头模式无限等待扫码。
                    return
            except Exception as exc:
                from core.errors import AdaptationRequired, RetryableError
                if isinstance(exc, AdaptationRequired):
                    store.set('desired', 'paused')
                    record(store, logger, '需要适配', str(exc))
                elif isinstance(exc, RetryableError):
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
