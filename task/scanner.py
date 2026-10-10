"""课程级并发扫描：实时进度、增量入库、部分失败和取消均有明确结果。"""
import asyncio
import time
from core.errors import Interrupted, LoginRequired, AdaptationRequired
from core.logging_setup import record


async def scan(adapter, store, config, logger, catalog_only=False):
    started = time.time()
    progress = dict(status='running', phase='验证登录', total=0, completed=0,
                    succeeded=0, failed=0, tasks=0, empty=0, current=[], started=started,
                    mode='catalog' if catalog_only else 'tasks')
    store.set('scan_progress', progress)
    store.set('scan_errors', [])
    store.set('scanned_account', None)
    active, errors, jobs = {}, [], []

    def publish():
        progress['current'] = list(active.values())
        progress['elapsed'] = round(time.time() - started, 1)
        store.set('scan_progress', progress)
        store.set('scan_errors', errors)

    def on_phase(course, phase):
        active[course['classroom_id']] = {'course': course['name'], 'phase': phase}
        publish()

    adapter.scan_progress = on_phase
    try:
        account = await adapter.account()
        store.set('account', account)
        progress['phase'] = '读取课程列表'
        publish()
        all_courses = await adapter.courses()
        store.set('course_count', len(all_courses))
        selected = store.selected_courses(account['id'])
        if catalog_only:
            courses = all_courses
            catalog = {c['classroom_id']: dict(c, start=None, end=None, date_error='等待读取') for c in courses}
            store.set('course_catalog:' + account['id'], list(catalog.values()))
            store.select_courses(set(selected) & set(catalog), account['id'])
        else:
            if not selected:
                raise AdaptationRequired('请先在“课程选择”中勾选课程，再重新扫描')
            courses = [c for c in all_courses if c['classroom_id'] in selected]
            if len(courses) != len(selected):
                raise AdaptationRequired('所选课程列表已变化，请刷新课程列表后重新选择')
        allowed = ('catalog',) if catalog_only else ('scan', 'run')
        progress.update(total=len(courses), phase='读取开课与结课时间' if catalog_only else '扫描所选课程')
        publish()
        semaphore = asyncio.Semaphore(int(config['scan_concurrency']))

        async def one(course):
            async with semaphore:
                if store.get('desired') not in allowed:
                    raise Interrupted()
                on_phase(course, '准备读取')
                try:
                    if catalog_only:
                        metadata = await adapter.course_metadata(course)
                        if store.get('desired') not in allowed:
                            raise Interrupted()
                        catalog[course['classroom_id']] = metadata
                        store.set('course_catalog:' + account['id'], list(catalog.values()))
                        progress['succeeded'] += 1
                        return
                    tasks = await adapter.scan_course(course)
                    if store.get('desired') not in ('scan', 'run'):
                        raise Interrupted()
                    store.merge_course(account['id'], course['classroom_id'], tasks)
                    progress['succeeded'] += 1
                    progress['tasks'] += len(tasks)
                    progress['empty'] += int(not tasks)
                    record(store, logger, '扫描完成', f'发现 {len(tasks)} 个章节叶子任务', {'course': course['name']})
                except (LoginRequired, Interrupted):
                    raise
                except Exception as exc:
                    reason = str(exc) if exc.__class__.__module__ == 'core.errors' else f'{type(exc).__name__}：课程扫描异常'
                    errors.append({'course': course['name'], 'reason': reason})
                    progress['failed'] += 1
                    if catalog_only:
                        catalog[course['classroom_id']]['date_error'] = reason
                        store.set('course_catalog:' + account['id'], list(catalog.values()))
                        record(store, logger, '课程时间读取失败', reason, {'course': course['name']})
                        return
                    for task in store.rows(account['id']):
                        if task['classroom_id'] == course['classroom_id'] and task['state'] != 'completed':
                            store.update(task['key'], state='needs_adapter', note='该课程本次扫描失败')
                    record(store, logger, '扫描失败', reason, {'course': course['name']})
                finally:
                    active.pop(course['classroom_id'], None)
                    progress['completed'] = progress['succeeded'] + progress['failed']
                    publish()

        jobs = [asyncio.create_task(one(c)) for c in courses]
        await asyncio.gather(*jobs)
        current = {c['classroom_id'] for c in all_courses}
        old_rows = store.rows(account['id'])
        with store.db() as db:
            for task in old_rows:
                if task['classroom_id'] not in current:
                    db.execute('UPDATE tasks SET active=0 WHERE key=?', (task['key'],))
        outcome = 'failed' if courses and progress['failed'] == len(courses) else 'partial' if errors else 'completed'
        progress.update(status=outcome, phase='扫描结束', finished=time.time())
        progress['message'] = (f"扫描{'失败' if outcome == 'failed' else '完成'}：{progress['completed']}/{len(courses)} 门，"
                               f"发现 {progress['tasks']} 个任务，失败 {progress['failed']} 门，空课程 {progress['empty']} 门")
        if outcome == 'failed':
            progress['message'] += '。无法判断是否有未完成任务，请查看扫描异常'
        elif not catalog_only:
            store.set('scanned_account', account['id'])
            store.set('scanned_courses', selected)
        if catalog_only:
            progress['message'] = (f"课程列表已更新：{len(courses)} 门，时间读取失败 {progress['failed']} 门。"
                                   '请勾选课程，再点击“重新扫描”；勾选自动保存。')
        store.set('scan_time', time.time())
        record(store, logger, '扫描结束', progress['message'])
        return progress
    except (Interrupted, asyncio.CancelledError):
        progress.update(status='cancelled', phase='扫描已暂停', message='扫描已暂停，已成功读取的任务已保存')
        raise
    except Exception as exc:
        reason = str(exc) if exc.__class__.__module__ == 'core.errors' else f'{type(exc).__name__}：扫描异常'
        progress.update(status='failed', phase='扫描失败', message=reason)
        raise
    finally:
        for job in jobs:
            if not job.done():
                job.cancel()
        await asyncio.gather(*jobs, return_exceptions=True)
        adapter.scan_progress = None
        active.clear()
        publish()
