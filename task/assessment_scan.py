"""Independent, read-only inventory across the account's course catalog."""
import asyncio
import hashlib

from core.errors import LoginRequired


async def scan_assessments(adapter, store, concurrency):
    report = store.get('assessment_inventory', {})
    if not report.get('id') or report.get('phase') not in ('queued', 'running'):
        return 'idle'

    report.update(phase='running', courses=0, finished=0, tasks=[], failed=[])
    completed = False

    def active():
        return store.get('desired') == 'assessment_scan'

    def publish():
        store.set('assessment_inventory', report)

    async def scan_course(course):
        async with semaphore:
            if not active():
                return
            try:
                tasks = await adapter.scan_course(course, assessments_only=True)
            except LoginRequired:
                raise
            except Exception:
                report['failed'].append(course['name'][:200])
            else:
                if active():
                    # No signed URL, response body, or answers in the inventory.
                    report['tasks'].extend({
                        'id': f"{task['classroom_id']}:{task['leaf_id']}",
                        'course': task['course'][:200],
                        'title': task['title'][:300],
                        'kind': task['kind'],
                        'state': task['state'],
                        'deadline': task['deadline'],
                    } for task in tasks)
            report['finished'] += 1
            publish()

    jobs = []
    try:
        publish()
        account = await adapter.account()
        if account['id'] != report['account']:
            raise LoginRequired('当前登录账号已变化')
        courses = await adapter.courses()
        if active():
            report['courses'] = len(courses)
            publish()
            semaphore = asyncio.Semaphore(int(concurrency))
            jobs = [asyncio.create_task(scan_course(course)) for course in courses]
            await asyncio.gather(*jobs)
            # A final account check prevents retaining a mixed inventory after a login change.
            await adapter.account()
            completed = active()
    except LoginRequired:
        report.update(tasks=[], failed=[], message='账号已变化或登录失效，请登录后重新扫描')
    except Exception:
        report['message'] = '课程目录读取失败，请检查浏览器连接后重试'
    finally:
        for job in jobs:
            if not job.done():
                job.cancel()
        await asyncio.gather(*jobs, return_exceptions=True)
        if not active():
            report.update(phase='cancelled', message='扫描已停止')
        elif completed:
            message = ('扫描结束，部分课程未能读取。' if report['failed'] else '课程清单已读取。')
            report.update(phase='done', message=message + '打开题页后才能判断是否适合 AI 作答。')
            report['tasks'].sort(key=lambda item: (item['course'], item['kind'], item['id']))
        else:
            report['phase'] = 'error'
        publish()
    return 'paused' if report.get('resume') == 'paused' else 'idle'


async def open_assessment(adapter, browser, store):
    """Consume one explicit selection before opening the exact assessment."""
    request = store.get('assessment_entry', {})
    resume = 'paused' if request.get('resume') == 'paused' else 'idle'
    if request.get('phase') != 'queued' or request.get('start_authorized') is not True:
        if request.get('phase') == 'opening':
            store.set('assessment_entry', {**request, 'phase': 'error', 'message': '任务操作曾中断，请重新选择。'})
        return resume

    def allowed():
        return store.get('desired') == 'assessment_open'

    def document_digest():
        return hashlib.sha256(browser.page.url.encode('utf-8')).hexdigest()

    store.set('assessment_entry', {**request, 'phase': 'opening'})
    result = {'phase': 'error', 'message': '未能核对任务入口。请在课程页找到该任务，开始后选择「填写当前题页」。'}
    try:
        account = await adapter.account()
        if account['id'] != request['account'] or not allowed():
            raise LoginRequired('账号或操作状态已变化')
        classroom_id, leaf_id = request['task_id'].split(':', 1)
        if not all(value.isascii() and value.isdecimal() for value in (classroom_id, leaf_id)):
            return resume
        tasks = await adapter.scan_course({'classroom_id': classroom_id, 'name': ''}, assessments_only=True)
        matches = [item for item in tasks if item['leaf_id'] == leaf_id]
        if len(matches) != 1 or matches[0]['state'] in ('locked', 'completed') or not allowed():
            result['message'] = '任务状态已变化，请重新扫描。'
            return resume
        task = matches[0]
        course_url = browser.page.url
        await browser.open_task(task, entry_allowed=allowed)
        started = await browser.start_assessment(task, course_url, allowed)
        if started:
            result = {'phase': 'opened', 'document': document_digest(), 'message': '已进入答题，正在读取题页。'}
        else:
            result = {'phase': 'awaiting_start', 'message': '尚未检测到可读题目。若网页要求额外验证，请处理后点「读取当前题页」。'}
    except LoginRequired:
        result['message'] = '登录状态已变化，请先在浏览器重新登录。'
    except Exception:
        pass
    finally:
        if not allowed():
            result = {'phase': 'cancelled', 'message': '已停止打开任务。'}
        store.set('assessment_entry', {**request, **result})
    return resume
