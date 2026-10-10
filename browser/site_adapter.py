"""雨课堂前端 1.2.321 的只读接口适配。

依据公开 pc.87fb3ab3.js；账号数据和校园部署仍需实站验收。
本模块不发送学习心跳或写入完成标记，所有学习上报由正常页面处理。
"""
import json
import math
import time
from urllib.parse import urlparse
from core.errors import LoginRequired, AdaptationRequired, RetryableError
from core.courses import timestamp
from core.task_types import task_kind, AUTOMATED_KINDS, manual_note


def identifier(value):
    text = str(value)
    if not text.isdigit():
        raise AdaptationRequired('服务器返回了未知格式的课程或任务 ID')
    return text


def completion(leaf_type, schedule):
    """返回 (完成状态, 显示比例)。只认可已核验的各类型字段语义。"""
    if leaf_type in (0, 1, 2, 3, 4, 8, 10):
        if isinstance(schedule, bool) or not isinstance(schedule, (float, int)):
            return None, None
        if not math.isfinite(schedule) or not 0 <= schedule <= 1:
            return None, None
        return (schedule >= .6 if leaf_type == 2 else schedule == 1), float(schedule)
    if leaf_type in (5, 9) and isinstance(schedule, dict):
        status = schedule.get('status')
        if type(status) is int:
            return status in (3, 4, 5), None
    if leaf_type in (6, 7) and isinstance(schedule, dict):
        done, total = schedule.get('done'), schedule.get('total')
        if type(done) is int and type(total) is int and 0 <= done <= total:
            return done > 0 and done == total, done / total if total else 0
    return None, None


def leaves(chapters):
    if not isinstance(chapters, list):
        raise AdaptationRequired('course_chapter 不是列表')
    for chapter in chapters:
        entries = chapter.get('section_leaf_list')
        if not isinstance(entries, list):
            raise AdaptationRequired('章节缺少 section_leaf_list，扫描不完整')
        for section in entries:
            children = section.get('leaf_list')
            if children is None:
                children = [section]
            if not isinstance(children, list):
                raise AdaptationRequired('leaf_list 不是列表')
            for leaf in children:
                if 'id' not in leaf or 'leaf_type' not in leaf:
                    raise AdaptationRequired('叶子节点缺少 id 或 leaf_type')
                yield leaf


class SiteAdapter:
    def __init__(self, browser, config):
        self.browser, self.config = browser, config
        self.account_id = None
        self.course_headers = {}
        self.course_context = {}
        self.course_dates = {}
        self.scan_progress = None

    def report(self, course, phase):
        if self.scan_progress:
            self.scan_progress(course, phase)

    async def context(self, cid):
        """与真实学生页面一致，先取课程访问参数；sign 不落日志。"""
        if cid not in self.course_context:
            data = await self.details(cid)
            if not isinstance(data, dict) or not data.get('course_sign'):
                raise AdaptationRequired('课程详情缺少 course_sign，无法读取章节及进度')
            self.course_context[cid] = {'cid': cid, 'classroom_id': cid,
                                        'sign': data['course_sign'], 'term': 'latest'}
            if data.get('uv_id'):
                self.course_headers[cid] = {'university-id': str(data['uv_id'])}
        return dict(self.course_context[cid])

    async def details(self, cid):
        data = await self.get(f'/v2/api/web/classrooms/{cid}', {'role': 5}, self.course_headers.get(cid))
        if not isinstance(data, dict):
            raise AdaptationRequired('课程详情结构未知')
        self.course_dates[cid] = {'start': timestamp(data.get('class_start')),
                                  'end': timestamp(data.get('class_end'))}
        return data

    async def course_metadata(self, course):
        await self.details(course['classroom_id'])
        # 只保存名称与时间；course_sign 不写入课程目录或日志。
        return {**course, **self.course_dates[course['classroom_id']], 'date_error': ''}

    async def get(self, path, params=None, headers=None, auth_probe=False):
        # 唯一出口为 GET。限定同一配置站点，拒绝跨站跳转获取凭据。
        url = self.config['base_url'] + path
        request_headers = {'Cache-Control': 'no-cache', 'xtbz': 'ykt', **(headers or {})}
        params = dict(params or {})
        if request_headers.get('university-id'):
            request_headers['uv-id'] = request_headers['university-id']
            params.setdefault('uv_id', request_headers['university-id'])
        try:
            response = await self.browser.context.request.get(
                url, params=params, headers=request_headers,
                timeout=self.config['request_timeout_seconds'] * 1000, max_redirects=0)
        except Exception as exc:
            raise RetryableError('网络请求失败或超时') from exc
        try:
            if response.status == 401:
                raise LoginRequired('登录已失效，请在 Edge 中重新登录')
            if response.status in (301, 302, 303, 307, 308):
                location = response.headers.get('location', '')
                if 'login' in location or 'passport' in location:
                    raise LoginRequired('接口跳转到登录页面')
                raise AdaptationRequired('接口发生未识别重定向')
            if response.status == 403:
                raise AdaptationRequired('接口拒绝访问（403），请检查课程权限或登录状态')
            if response.status == 429 or response.status >= 500:
                raise RetryableError(f'服务器暂不可用 HTTP {response.status}')
            if response.status != 200:
                raise AdaptationRequired(f'接口 HTTP {response.status}: {path}')
            try:
                payload = await response.json()
            except Exception as exc:
                if auth_probe:
                    raise LoginRequired('账号接口未返回 JSON，请先登录') from exc
                raise AdaptationRequired(f'接口未返回 JSON: {path}') from exc
            if not isinstance(payload, dict):
                raise AdaptationRequired('接口响应不是对象')
            # 此入口实站返回裸对象，与 errcode/data 包装的课程接口不同。
            if path == '/edu_admin/leaf_level_info/' and all(k in payload for k in ('classroom_id', 'courseware_id', 'activity_id')):
                return payload
            ok = payload.get('success') is True or type(payload.get('errcode')) is int and payload['errcode'] == 0
            if not ok:
                if auth_probe:
                    raise LoginRequired('尚未登录或账号接口未通过验证')
                raise AdaptationRequired(f'接口未返回成功标识: {path}')
            return payload.get('data')
        finally:
            # APIResponse 会缓存响应体；长期运行需主动释放。
            await response.dispose()

    async def account(self):
        data = await self.get('/v2/api/web/userinfo', auth_probe=True)
        if not isinstance(data, list) or not data or not isinstance(data[0], dict):
            raise AdaptationRequired('账号接口结构已变化')
        user = data[0]
        if not user.get('user_id'):
            raise LoginRequired('账号接口缺少 user_id')
        current = str(user['user_id'])
        if self.account_id and self.account_id != current:
            raise LoginRequired('检测到切换账号，请重新扫描后再开始')
        self.account_id = current
        return {'id': current, 'name': str(user.get('name') or user.get('nickname') or current)}

    async def courses(self):
        data = await self.get('/v2/api/web/courses/list', {'identity': 2})
        if not isinstance(data, dict) or not isinstance(data.get('list'), list):
            raise AdaptationRequired('课程列表结构已变化')
        # 当前前端一次读取整个 list，若平台以后分页，明确报不完整。
        if data.get('has_next') or data.get('next') or (isinstance(data.get('total'), int) and data['total'] > len(data['list'])):
            raise AdaptationRequired('课程接口出现分页，需要适配分页后才能保证完整扫描')
        result = []
        self.course_context.clear()
        self.course_dates.clear()
        for item in data['list']:
            cid = identifier(item.get('classroom_id'))
            course = item.get('course', {})
            if not isinstance(course, dict):
                raise AdaptationRequired('课程元数据结构未知')
            # 可配置读接口请求头；不修改浏览器内的全局学校选择状态。
            headers = {}
            if course.get('university_id'):
                headers['university-id'] = str(course['university_id'])
            self.course_headers[cid] = headers
            result.append({'classroom_id': cid, 'name': str(course.get('name') or item.get('name') or cid),
                           'class_name': str(item.get('name') or ''), 'term': str(item.get('term') or '')})
        return result

    async def schedule(self, cid):
        params = await self.context(cid)
        data = await self.get('/mooc-api/v1/lms/learn/course/schedule', params, self.course_headers.get(cid))
        if not isinstance(data, dict) or not isinstance(data.get('leaf_schedules'), dict):
            raise AdaptationRequired('缺少服务器 leaf_schedules，无法确认完成')
        return data['leaf_schedules']

    async def scan_course(self, course):
        cid = course['classroom_id']
        self.report(course, '读取课程访问参数')
        params = await self.context(cid)
        self.report(course, '读取章节目录')
        data = await self.get('/mooc-api/v1/lms/learn/course/chapter', params, self.course_headers.get(cid))
        if not isinstance(data, dict) or 'course_chapter' not in data:
            raise AdaptationRequired('该课程不是已适配的 LMS 章节课程，需人工查看')
        nodes = list(leaves(data['course_chapter']))
        if not nodes:
            return []  # 目录确认为空时，不调用不适用的进度接口。
        self.report(course, '读取服务器进度')
        schedules = await self.schedule(cid)
        tasks = []
        for leaf in nodes:
            lid = identifier(leaf['id'])
            typ = leaf['leaf_type']
            if type(typ) is not int:
                raise AdaptationRequired('任务类型字段不是整数')
            done, progress = completion(typ, schedules.get(lid))
            kind = task_kind(typ)
            state = 'completed' if done else 'locked' if leaf.get('is_locked') else 'manual' if kind not in AUTOMATED_KINDS else 'pending'
            note = manual_note(typ) if state == 'manual' else ''
            if done is None and kind in AUTOMATED_KINDS and state != 'locked':
                state, note = 'needs_adapter', '服务器未提供该任务的可识别进度，不能自动判定'
            deadline = leaf.get('score_deadline')
            if not isinstance(deadline, (int, float)) or isinstance(deadline, bool):
                deadline = None
            elif deadline > 1e12:
                deadline /= 1000
            tasks.append(dict(key=f'{self.account_id}:{cid}:{lid}', classroom_id=cid,
                              leaf_id=lid, course=course['name'], title=str(leaf.get('name') or lid),
                              leaf_type=typ, kind=kind, state=state, progress=progress,
                              deadline=deadline, duration=None, note=note,
                              evidence=self.evidence(cid, lid, schedules[lid]) if done else ''))
        return tasks

    def evidence(self, cid, lid, value):
        return json.dumps({'source': '/mooc-api/v1/lms/learn/course/schedule',
                           'account': self.account_id, 'classroom_id': cid, 'leaf_id': lid,
                           'value': value, 'observed_at': time.time()}, ensure_ascii=False)

    async def status(self, task):
        await self.account()  # 防止运行中换号后把另一个账号的完成状态写入当前任务。
        schedules = await self.schedule(task['classroom_id'])
        value = schedules.get(task['leaf_id'])
        done, progress = completion(task['leaf_type'], value)
        if done is None:
            raise AdaptationRequired('当前任务服务器进度缺失或含义未知')
        return done, progress, self.evidence(task['classroom_id'], task['leaf_id'], value)

    async def prepare_slides(self, task):
        """取得网站实际 PPT 入口及页数，只读，不构造阅读上报。"""
        entry = await self.get('/edu_admin/leaf_level_info/', {'leaf_level_id': task['leaf_id']})
        if not isinstance(entry, dict):
            raise AdaptationRequired('课件入口结构未知')
        cid = identifier(entry.get('classroom_id'))
        cards = identifier(entry.get('courseware_id'))
        activity = identifier(entry.get('activity_id'))
        if cid != task['classroom_id']:
            raise AdaptationRequired('课件入口所属课程与任务不一致')
        data = await self.get('/v2/api/web/cards/detlist/' + cards, {'classroom_id': cid})
        slides = data.get('Slides') if isinstance(data, dict) else None
        if not isinstance(slides, list) or not slides:
            raise AdaptationRequired('课件没有可识别的 PPT 页面')
        if [p.get('PageIndex') for p in slides] != list(range(1, len(slides)+1)):
            raise AdaptationRequired('PPT 页码结构变化，不能保证逐页处理')
        if any(p.get('Problem') or p.get('problem_detail') for p in slides):
            raise AdaptationRequired('课件含答题页，请先在网页完成题目；不会自动作答或提交')
        return {'classroom_id': cid, 'cards_id': cards, 'total': len(slides),
                'path': f'/v2/web/studentCards/{cid}/{cards}/{activity}/ppt'}

    async def slide_depth(self, info):
        data = await self.get('/v2/api/web/cards/view_depth',
                              {'classroom_id': info['classroom_id'], 'cards_id': info['cards_id']})
        pages = data.get('depth_detail') if isinstance(data, dict) else None
        if not isinstance(pages, list) or any(type(p) is not int or not 0 <= p < info['total'] for p in pages):
            raise AdaptationRequired('服务器未返回可识别的 PPT 已读页码')
        return set(pages)
