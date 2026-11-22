import asyncio
from collections import Counter
from core.errors import Interrupted, LoginRequired, AdaptationRequired, OutsideStudyWindow
from core.courses import window_reason
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
    catalog = {str(c['classroom_id']): c for c in store.catalog(account['id'])}
    def unavailable(task):
        return window_reason(catalog.get(task['classroom_id']), task)
    scope = store.get('task_scope')
    tasks = store.scoped_rows(account['id'], include_unavailable=True, include_inactive=scope is not None)
    if scope is not None:
        by_id = {t['key']: t for t in tasks}
        ids = scope.get('ids') if isinstance(scope, dict) else None
        if (not isinstance(ids, list) or not ids or not all(isinstance(key, str) for key in ids)
                or scope.get('account') != account['id'] or scope.get('courses') != store.selected_courses(account['id'])
                or len(ids) != len(set(ids)) or any(key not in by_id or by_id[key]['kind'] not in AUTOMATED_KINDS
                    or (not unavailable(by_id[key]) and (not by_id[key]['active']
                        or by_id[key]['state'] not in ('pending', 'running', 'completed'))) for key in ids)):
            store.set('desired', 'paused')
            record(store, logger, '队列暂停', '所选任务范围已变化或有任务不可执行，请重新勾选；不会执行其他任务')
            return
        tasks = [by_id[key] for key in ids]
    blocked_key = store.get('blocked_task')
    blocked = next((t for t in tasks if t['key'] == blocked_key), None)
    if blocked and not unavailable(blocked) and blocked['state'] not in ('completed', 'pending'):
        store.set('desired', 'paused')
        record(store, logger, '队列暂停', '上次中断任务尚未解决，请重试或修正适配后重新扫描', blocked)
        return
    store.set('blocked_task', None)
    processed, confirmed = 0, 0
    for task in tasks:
        if store.get('desired') != 'run':
            raise Interrupted()
        reason = unavailable(task)
        if reason or not task['active']:
            if task['state'] != 'completed':
                record(store, logger, '跳过任务', reason or '任务已移出章节', task)
            continue
        if task['state'] not in ('pending', 'running') or task['kind'] not in AUTOMATED_KINDS:
            continue
        if task['attempts'] >= config['max_task_attempts']:
            store.update(task['key'], state='failed', note='重试预算已耗尽，请选中任务后重置重试')
            store.set('blocked_task', task['key'])
            store.set('desired', 'paused')
            return
        processed += 1
        while task['attempts'] < config['max_task_attempts']:
            reason = unavailable(task)
            if reason:
                record(store, logger, '跳过任务', reason, task)
                break
            if store.get('desired') != 'run':
                raise Interrupted()
            task['attempts'] += 1
            store.update(task['key'], state='running', attempts=task['attempts'], note='')
            record(store, logger, '执行', f"尝试 {task['attempts']}", task)
            committed = False
            try:
                task['_course_window'] = catalog.get(task['classroom_id'], {})
                evidence = await execute(task, adapter, browser, store, config)
                store.update(task['key'], state='completed', progress=1, evidence=evidence,
                             note='服务器进度已确认')
                record(store, logger, '完成', '已复查服务器任务记录', task)
                confirmed += 1
                committed = True
                # 完成后退出任务页面，阻止网站自行连续播放到其他任务。
                await browser.page.goto(config['base_url'] + config['entry_path'], wait_until='domcontentloaded')
                break
            except OutsideStudyWindow as exc:
                dates = getattr(adapter, 'course_dates', {}).get(task['classroom_id'])
                if dates and task['classroom_id'] in catalog:
                    catalog[task['classroom_id']].update({k: v for k, v in dates.items() if v is not None})
                    store.set('course_catalog:' + account['id'], list(catalog.values()))
                store.update(task['key'], state='pending', attempts=task['attempts'] - 1, note=str(exc))
                record(store, logger, '跳过任务', str(exc), task)
                store.set('current', {})
                # Deadline cancellation can interrupt the page handler's own cleanup.
                # Stop autoplay even when this was the last item in the queue.
                try:
                    await asyncio.wait_for(browser.pause_media(), 3)
                except Exception:
                    pass  # A closed or unresponsive player can still be navigated away from.
                try:
                    await browser.page.goto(config['base_url'] + config['entry_path'],
                                            wait_until='domcontentloaded', timeout=5000)
                except Exception:
                    record(store, logger, '页面退出失败', '已跳过截止任务，下项将重新打开页面', task)
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
                    if unavailable(task):
                        break
                    if store.get('desired') != 'run':
                        raise Interrupted()
                    await asyncio.sleep(.5)
                if unavailable(task):
                    continue
                if browser.page.is_closed() or not browser.context.browser.is_connected():
                    # 交给进程守护重建 Edge，数据库已保存任务及尝试次数。
                    raise RuntimeError('Edge 已关闭，需要进程重建')
                await browser.page.goto(config['base_url'] + config['entry_path'], wait_until='domcontentloaded')
    summary = None
    if store.get('desired') == 'run':
        store.set('desired', 'idle')
        chosen_keys = {t['key'] for t in tasks}
        remaining = [t for t in store.scoped_rows() if t['state'] != 'completed' and (scope is None or t['key'] in chosen_keys)]
        counts = Counter(t['state'] for t in remaining)
        labels = {'locked': '锁定', 'needs_adapter': '需要适配', 'manual': '需手动完成',
                  'failed': '失败', 'pending': '待执行', 'running': '执行中'}
        details = '、'.join(f'{labels.get(state, "其他状态")} {count} 项'
                           for state, count in sorted(counts.items()))
        message = (f'本轮处理 {processed} 项，服务器确认完成 {confirmed} 项'
                   if processed else '本轮未执行任务')
        message += f'；剩余 {len(remaining)} 项未完成'
        if details:
            message += f'（{details}）'
        summary = dict(processed=processed, confirmed=confirmed, remaining=len(remaining), message=message)
        record(store, logger, '队列结束', message)
    store.set('current', {})
    return summary
