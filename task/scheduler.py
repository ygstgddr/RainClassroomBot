import asyncio
from core.errors import Interrupted, LoginRequired, AdaptationRequired
from core.logging_setup import record
from task.executor import execute
from core.task_types import AUTOMATED_KINDS


async def run_queue(adapter, browser, store, config, logger):
    if store.get('scan_progress', {}).get('status') in ('queued', 'running', 'failed', 'cancelled'):
        store.set('desired', 'paused')
        record(store, logger, '队列暂停', '扫描尚未完整结束或扫描失败，请重新扫描')
        return
    account = await adapter.account()
    if not store.scope_scanned(account['id']):
        store.set('desired', 'paused')
        record(store, logger, '队列暂停', '课程选择已改变或尚未选择，请重新扫描所选课程')
        return
    await adapter.courses()  # 重新建立每门课程的请求上下文。
    tasks = store.scoped_rows(account['id'])
    blocked_key = store.get('blocked_task')
    blocked = next((t for t in tasks if t['key'] == blocked_key), None)
    if blocked and blocked['state'] not in ('completed', 'pending'):
        store.set('desired', 'paused')
        record(store, logger, '队列暂停', '上次中断任务尚未解决，请重试或修正适配后重新扫描', blocked)
        return
    store.set('blocked_task', None)
    for task in tasks:
        if store.get('desired') != 'run':
            raise Interrupted()
        if task['state'] not in ('pending', 'running') or task['kind'] not in AUTOMATED_KINDS:
            continue
        if task['attempts'] >= config['max_task_attempts']:
            store.update(task['key'], state='failed', note='重试预算已耗尽，请选中任务后重置重试')
            store.set('blocked_task', task['key'])
            store.set('desired', 'paused')
            return
        while task['attempts'] < config['max_task_attempts']:
            if store.get('desired') != 'run':
                raise Interrupted()
            task['attempts'] += 1
            store.update(task['key'], state='running', attempts=task['attempts'], note='')
            record(store, logger, '执行', f"尝试 {task['attempts']}", task)
            committed = False
            try:
                evidence = await execute(task, adapter, browser, store, config)
                store.update(task['key'], state='completed', progress=1, evidence=evidence,
                             note='服务器进度已确认')
                record(store, logger, '完成', '已复查服务器任务记录', task)
                committed = True
                # 完成后退出任务页面，阻止网站自行连续播放到其他任务。
                await browser.page.goto(config['base_url'] + config['entry_path'], wait_until='domcontentloaded')
                break
            except (Interrupted, LoginRequired):
                store.update(task['key'], state='pending', attempts=task['attempts'] - 1,
                             note='已暂停；恢复时重新查询服务器')
                raise
            except AdaptationRequired as exc:
                store.update(task['key'], state='needs_adapter', note=str(exc))
                store.set('blocked_task', task['key'])
                record(store, logger, '需要适配', str(exc), task)
                store.set('desired', 'paused')
                return
            except Exception as exc:
                if committed:
                    # 已取得服务器证据，退出页面失败不应撤销完成记录。
                    raise RuntimeError('任务已确认完成，返回列表失败，需要重建浏览器') from exc
                # 不记录可能携带敏感 URL 的底层 Playwright 异常全文。
                message = str(exc) if exc.__class__.__module__ == 'core.errors' else f'{type(exc).__name__}：浏览器或页面异常'
                record(store, logger, '重试', message, task)
                store.update(task['key'], state='pending', note=message)
                if task['attempts'] >= config['max_task_attempts']:
                    store.update(task['key'], state='failed')
                    store.set('blocked_task', task['key'])
                    store.set('desired', 'paused')
                    record(store, logger, '队列暂停', '当前任务未完成，不进入下一任务', task)
                    return
                # 有限指数退避；每 0.5 秒检查用户控制。
                for _ in range(min(60, 2 ** task['attempts'] * 2)):
                    if store.get('desired') != 'run':
                        raise Interrupted()
                    await asyncio.sleep(.5)
                if browser.page.is_closed() or not browser.context.browser.is_connected():
                    # 交给进程守护重建 Edge，数据库已保存任务及尝试次数。
                    raise RuntimeError('Edge 已关闭，需要进程重建')
                await browser.page.goto(config['base_url'] + config['entry_path'], wait_until='domcontentloaded')
    if store.get('desired') == 'run':
        store.set('desired', 'idle')
        remaining = [t for t in store.scoped_rows() if t['state'] != 'completed']
        record(store, logger, '队列结束', f'自动队列已处理；仍有 {len(remaining)} 个锁定、未适配或人工任务')
    store.set('current', {})
