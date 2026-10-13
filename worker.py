"""独立工作进程；Playwright 全部操作归属于同一 asyncio 事件循环。"""
import asyncio
import time
from queue import Empty
from browser.edge_manager import EdgeManager
from browser.site_adapter import SiteAdapter
from core.errors import LoginRequired, Interrupted
from core.logging_setup import make_logger, record
from core.store import Store
from core.workflow import Workflow


TYPING_ACTIVE = frozenset(('selecting', 'ready', 'running', 'paused'))


class TypingCommands:
    """把 GUI 的易失命令送给 Playwright 绑定元素；只持久化摘要与计数。

    队列和文本的寿命属于当前 worker，浏览器崩溃不会自动续打。学习任务
    必须完全暂停后才能选择目标；反向也会锁定学习操作，避免导航串框。
    """
    def __init__(self, browser, store, config, engine_factory=None):
        from browser.manual_typing import ManualTyping
        self.store = store
        self.commands = config.get('typing_commands')
        self.generation = config.get('typing_generation', '')
        self.request_id = ''
        self.engine = (engine_factory or ManualTyping)(browser, on_update=self.publish)
        self.publish(self.engine.snapshot())

    def publish(self, state):
        # 不信任引擎扩展的额外字段：用户原文/输入框现有全文不可进 SQLite。
        target = state.get('target')
        public_target = None
        if isinstance(target, dict):
            public_target = {key: target[key] for key in
                ('token', 'label', 'url', 'frame', 'tag', 'type', 'characters', 'multiline') if key in target}
        public = {'phase': state.get('phase', 'idle'), 'target': public_target,
            'sent': state.get('sent', 0), 'total': state.get('total', 0),
            'speed': state.get('speed', 20), 'message': str(state.get('message', ''))[:700],
            'connected': state.get('connected', True), 'generation': self.generation, 'request_id': self.request_id}
        self.store.set('typing_status', public)

    @property
    def active(self):
        return self.engine.snapshot().get('phase') in TYPING_ACTIVE

    async def drain(self):
        if self.commands is None:
            return
        # 有界批次让 heartbeat/probe 始终有机会运行。
        for _ in range(16):
            try:
                command = self.commands.get_nowait()
            except Empty:
                break
            except (ValueError, OSError):
                break
            if not isinstance(command, dict) or command.get('generation') != self.generation:
                continue
            self.request_id = str(command.get('request_id', ''))[:64]
            try:
                name = command.get('action')
                if name not in ('typing_stop', 'typing_cancel') and (
                        self.store.get('desired', 'idle') not in ('idle', 'paused')
                        or self.store.get('worker_busy', False)):
                    raise ValueError('请先暂停并等待学习任务停止')
                if name == 'typing_select':
                    await self.engine.select()
                elif name == 'typing_start':
                    await self.engine.start(command.get('token'), command.get('text'),
                                            speed=command.get('speed', 20),
                                            confirmed=command.get('confirmed') is True)
                elif name == 'typing_pause':
                    await self.engine.pause()
                elif name == 'typing_resume':
                    await self.engine.resume(command.get('token'))
                elif name == 'typing_speed':
                    await self.engine.set_speed(command.get('speed'))
                elif name == 'typing_cancel':
                    await self.engine.cancel()
                elif name == 'typing_stop':
                    await self.engine.stop()
                else:
                    raise ValueError('未知打字操作')
                self.publish(self.engine.snapshot())
            except Exception as exc:
                # 引擎负责已失效目标的清理；参数拒绝不破坏原绑定，也不写原文。
                state = dict(self.engine.snapshot())
                from browser.manual_typing import ManualTypingError
                state['message'] = ('请先暂停并等待学习任务停止' if self.store.get('desired') not in ('idle', 'paused')
                                    else str(exc) if isinstance(exc, ManualTypingError)
                                    else f'操作未执行（{type(exc).__name__}），请核对指定目标后重试')
                self.publish(state)
            finally:
                command.clear()  # 尽早解除当前命令与用户原文的引用。

    async def close(self):
        await self.engine.stop()
        self.publish({'phase': 'idle', 'target': None, 'connected': False,
                      'message': '浏览器已断开，请重新指定输入框'})


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
    typing = None
    try:
        store.set('worker_status', '正在启动 Edge')
        headed = store.get('force_headed', False)
        await browser.start(headed=headed)
        browser.actual_headless = bool(config['headless'] and not headed)
        typing = TypingCommands(browser, store, config)
        adapter = SiteAdapter(browser, config)
        flow = Workflow(adapter, browser, store, config, logger)
        await browser.pause_media()
        previous = None
        while store.get('desired') != 'shutdown':
            await typing.drain()
            desired = store.get('desired', 'idle')
            if desired not in ('idle', 'paused') and typing.active:
                # 防御旧 GUI/其它进程直接写 desired：任何学习导航前先取消绑定。
                await typing.engine.stop()
            if desired in ('idle', 'paused'):
                flow.clear_login()
                if desired != previous:
                    phase = store.get('workflow', {}).get('phase')
                    if phase not in ('ready', 'paused', 'error', 'account_changed'):
                        flow.show('paused' if desired == 'paused' else 'ready',
                                  '已暂停，点击继续即可恢复' if desired == 'paused' else '选好课程后即可开始')
                previous = desired
                await asyncio.sleep(.1 if typing.active else .25)
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
        if typing:
            await typing.close()
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
