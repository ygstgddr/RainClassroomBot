"""A lightweight, task-scoped browser tool session over the existing WebView2 CDP.

The model sees observation references, never executable JS or a CDP endpoint.
All operations use the user's existing page and preserve the login profile.
"""
from contextlib import contextmanager
from copy import deepcopy
import re
import time
from urllib.parse import urlsplit
from uuid import uuid4

from playwright.sync_api import sync_playwright, Error as BrowserError
from browser.assessment_page import (adapt_groups, normalize_capture, question_script,
    surface_script, fill_script, verify_fill_script)
from browser.assessment_route import assessment_origin
from browser.assessment_submit import submission_script


class AgentBrowser:
    def __init__(self, page, home, cancelled):
        self.page, self.home, self.cancelled = page, home, cancelled
        self.initial_url = page.url
        self.exam_id = re.search(r'/(?:exam|start|cover)/(?:\d+/)?(\d+)(?:/|$)', urlsplit(page.url).path)
        self.exam_id = self.exam_id.group(1) if self.exam_id else None
        self.registry = '_rain_agent_' + uuid4().hex
        self.refs = {}
        self.capture = self.surface = None
        self._signature = None
        self._revision = 0

    @property
    def navigation_revision(self):
        try:
            signature = (self.page.url, self.page.evaluate('performance.timeOrigin'))
        except Exception:
            signature = ('unavailable',)
        if signature != self._signature:
            self._signature = signature
            self._revision += 1
        return self._revision

    def _check(self):
        if self.cancelled() or self.page.is_closed():
            raise ValueError('浏览器操作已停止。')
        url = self.page.url
        origin = assessment_origin(url, self.home)
        if not origin:
            raise ValueError('当前页面不属于授权的课程站点。')
        current = urlsplit(url)
        original = urlsplit(self.initial_url)
        if (current.scheme, current.netloc, current.path) != (original.scheme, original.netloc, original.path):
            match = re.search(r'/(?:exam|start|cover)/(?:\d+/)?(\d+)(?:/|$)', current.path)
            if not self.exam_id or not match or match.group(1) != self.exam_id:
                raise ValueError('页面离开了本次授权的作业范围。')
        return origin

    def _eval(self, script):
        self._check()
        try:
            result = self.page.evaluate(script)
        except BrowserError:
            raise ValueError('网页正在切换或控件已失效，请重新观察后重试。') from None
        if not isinstance(result, dict) or result.get('error'):
            raise ValueError(result.get('error', '页面未返回有效结果。') if isinstance(result, dict) else '页面未返回有效结果。')
        return result

    def observe(self):
        """Use live DOM handles including open shadow roots; expire old refs."""
        self._check()
        for handle in self.refs.values():
            try: handle.dispose()
            except Exception: pass
        self.refs = {}
        generation = uuid4().hex[:8]
        controls = []
        for handle in self.page.locator('button, a, input:not([type=hidden]), textarea, select, [role=button], [role=radio], [role=checkbox], [contenteditable=true], div, span').element_handles()[:800]:
            if not handle.is_visible():
                continue
            candidate = handle.evaluate('''el => {
                if (el.parentElement?.closest('button,a,[role=button],[role=radio],[role=checkbox]')) return false;
                return el.matches('button,a,input,textarea,select,[role=button],[role=radio],[role=checkbox],[contenteditable=true]') ||
                (getComputedStyle(el).cursor==='pointer' && el.innerText?.trim().length>0 && el.innerText.trim().length<80 &&
                 !el.querySelector('button,a,input,textarea,select,[role=button]') && el.children.length<3)
            }''')
            if not candidate:
                continue
            data = handle.evaluate('''el => {const r=el.getBoundingClientRect();
                const inViewport=r.bottom>0&&r.top<innerHeight&&r.right>0&&r.left<innerWidth;
                const hit=inViewport?document.elementFromPoint(Math.max(0,Math.min(innerWidth-1,r.left+r.width/2)),Math.max(0,Math.min(innerHeight-1,r.top+r.height/2))):null;
                return {tag:el.tagName.toLowerCase(), role:el.getAttribute('role')||'',
                type:el.type||'', label:(el.getAttribute('aria-label')||el.innerText||el.placeholder||el.title||'').trim().slice(0,180),
                disabled:!!el.disabled, in_viewport:inViewport, obscured:inViewport&&!!hit&&!el.contains(hit),
                value:el.matches('input:not([type=password]), textarea')?el.value.slice(0,1000):undefined}}''')
            if data['type'] in ('password', 'file'):
                continue
            ref = f'{generation}:{len(controls)+1}'
            self.refs[ref] = handle
            controls.append(dict(ref=ref, **data))
            if len(controls) >= 100:
                break
        return {'title': self.page.title(), 'url': self.page.url.split('?')[0],
                'text': self.page.locator('body').inner_text(timeout=5000)[:22000],
                'controls': controls, 'revision': self.navigation_revision,
                'note': '操作引用仅本次观察有效；需写答案时使用复核填写工具，交卷使用提交工具。'}

    def interact(self, action, ref=None, direction=None):
        self._check()
        if action == 'scroll':
            if direction not in ('up', 'down'):
                raise ValueError('滚动方向无效。')
            self.page.mouse.wheel(0, 600 if direction == 'down' else -600)
            self.page.wait_for_timeout(250)
            return self.observe()
        handle = self.refs.get(ref)
        if not handle or not handle.is_visible() or not handle.is_enabled():
            raise ValueError('目标已过期或不可操作，请重新观察。')
        info = handle.evaluate('''el=>({label:(el.innerText||el.getAttribute('aria-label')||el.value||'').trim(),
            tag:el.tagName,type:el.type,href:el.href||'',answer:!!el.closest('fieldset,[data-question-id],section.subject-item')})''')
        # Answer writes and final submission have a separate reviewed path.
        if (info['answer'] or info['tag'] in ('INPUT', 'TEXTAREA', 'SELECT')
                or re.search(r'交卷|提交|删除|退出|注销|下载|上传|支付|购买', info['label'])
                or info['href'] and not info['href'].startswith(self.initial_url.split('?')[0] + '#')):
            raise ValueError('该控件不能通过普通点击执行；答题请使用复核填写，交卷请使用核对提交工具。')
        before = self.navigation_revision
        clicked = False
        try:
            handle.click(timeout=5000)
            clicked = True
            if self.exam_id and re.fullmatch(r'开始(?:答题|考试|测验|作答|练习)?', info['label']):
                self.page.wait_for_url(re.compile(r'/exam/' + re.escape(self.exam_id) + r'(?:[/?#]|$)'), timeout=20000)
            self.page.wait_for_load_state('domcontentloaded', timeout=12000)
            self.page.wait_for_function("document.body && document.body.innerText.trim() && !/^正在加载/.test(document.body.innerText.trim())", timeout=12000)
            self.page.wait_for_timeout(300)
        except BrowserError:
            self._check()
            self.navigation_revision
            if not clicked:
                return {'error':'点击未发生：目标被遮挡或不在可操作区域，请重新观察定位。'}
            return {'state':'loading','message':'操作已发送，网页还在加载。请重新观察，不要重复点击。'}
        self._check()
        if self.navigation_revision != before:
            self.capture = self.surface = None
        return self.observe()

    def read_assessment_surface(self):
        result = self._eval(surface_script(self._check(), self.registry))
        self.surface = {**result, 'revision': self.navigation_revision}
        return deepcopy(self.surface)

    def read_assessment_page(self, surface=None, groups=None):
        proposals = None
        if surface is not None:
            if surface != self.surface:
                raise ValueError('页面观察已过期。')
            proposals = adapt_groups(surface, groups)
        result = self._eval(question_script(self._check(), self.registry, proposals))
        questions, warnings = normalize_capture(result)
        capture = questions, warnings, self.navigation_revision
        self.capture = deepcopy(capture), result, proposals
        return capture

    def revalidate_assessment_page(self, capture):
        if not self.capture or capture != self.capture[0] or capture[2] != self.navigation_revision:
            raise ValueError('题页已变化，请重新读取。')
        _, expected, proposals = self.capture
        if self._eval(question_script(self._check(), self.registry, proposals)) != expected:
            raise ValueError('题干或控件已变化，请重新读取。')
        return expected, proposals

    def fill_assessment_answers(self, capture, answers, *, cancelled):
        expected, proposals = self.revalidate_assessment_page(capture)
        if cancelled(): raise ValueError('操作已停止。')
        native, errors = [], {}
        remaining = dict(answers)
        for index, question in enumerate(capture[0]):
            qid = question['id']
            if qid not in answers or question['kind'] != 'single' or question['issues']:
                continue
            option = next((i for i, value in enumerate(question['options']) if value['id'] == answers[qid]), None)
            if option is None:
                continue
            identity = expected['identities'][index][2][option]
            label = self.page.evaluate_handle('''args => {
                const key=window[args.registry];
                const input=[...document.querySelectorAll('input[type=radio]')].find(el=>'c'+key(el)===args.id);
                const label=input?.closest('label.el-radio[role=radio]');
                return label?.__vue__?.$options?.name==='ElRadio' ? label : null;
            }''', {'registry':self.registry,'id':identity}).as_element()
            if label is None:
                continue
            remaining.pop(qid)
            self.revalidate_assessment_page(capture)
            if cancelled(): raise ValueError('操作已停止。')
            status = label.evaluate('''el=>({current:el.__vue__.model,desired:el.__vue__.label,
                disabled:el.__vue__.isDisabled || el.getAttribute('aria-disabled')==='true'})''')
            if status['current'] not in (None, '', status['desired']):
                errors[qid] = '已保留页面上的现有答案。'
                continue
            if status['disabled']:
                errors[qid] = '选项被网站禁用。'
                continue
            try:
                if status['current'] != status['desired']:
                    label.click(timeout=5000)
                self.page.wait_for_function('el=>el.isConnected && el.__vue__.model===el.__vue__.label', arg=label, timeout=5000)
                native.append(qid)
            except BrowserError as exc:
                errors[qid] = ('选项被页面浮层遮挡。请观察并关闭展开的答题卡或遮罩，再重试此题。'
                    if 'intercepts pointer events' in str(exc) else '点击后网页未确认选中，请重新观察控件状态再重试。')
        result = (self._eval(fill_script(self._check(), self.registry, proposals, expected, remaining))
                  if remaining else {'filled': [], 'errors': {}})
        result['filled'].extend(native)
        result['errors'].update(errors)
        if pending := result.pop('pending', None):
            self.page.wait_for_timeout(150)
            confirmed = self._eval(verify_fill_script(self._check(), self.registry, proposals, expected, pending))
            result['filled'].extend(confirmed['filled'])
            for qid in set(pending) - set(confirmed['filled']):
                result['errors'][qid] = '页面未确认写入。'
        return result

    def submit_assessment_answers(self, capture, answers, *, cancelled):
        expected, proposals = self.revalidate_assessment_page(capture)
        if (capture[1] or any(q['issues'] for q in capture[0]) or set(answers) != {q['id'] for q in capture[0]}):
            return {'state':'blocked', 'message':'题目未完整读取或复核，不能提交。'}
        deadline, begin = time.monotonic()+20, True
        while time.monotonic() < deadline:
            if not begin and (receipt := self.submission_receipt()):
                return receipt
            if cancelled(): return {'state':'unknown', 'message':'操作已停止，请核对网站提交结果。'}
            try:
                result = self._eval(submission_script(self._check(), self.registry, proposals, expected, answers, begin=begin))
            except Exception:
                if receipt := self.submission_receipt():
                    return receipt
                return {'state':'unknown', 'message':'提交后页面发生变化，请重新扫描确认；不会重复交卷。'}
            if result.get('state') != 'waiting': return result
            begin = False
            self.page.wait_for_timeout(250)
        return {'state':'unknown', 'message':'等待提交回执超时，不会重复交卷。'}

    def submission_receipt(self):
        """Recognize the real site's post-submit cover for this exact exam ID."""
        if self.cancelled() or not self.exam_id:
            return None
        try:
            self._check()
            if urlsplit(self.page.url).path.rstrip('/') != '/cover/' + self.exam_id:
                return None
            self.page.wait_for_load_state('domcontentloaded', timeout=4000)
            marker = self.page.locator('img[alt="已交卷"]')
            marker.wait_for(state='visible', timeout=4000)
            text = self.page.locator('body').inner_text(timeout=2000)
            if not all(label in text for label in ('试卷得分','未作答','作答次数')):
                return None
            score = re.search(r'(\d+(?:\.\d+)?)\s*/\s*(\d+(?:\.\d+)?)\s*试卷得分', text)
            return {'state':'submitted','message':'网站成绩页已确认交卷。' + (f' 得分 {score[1]}/{score[2]}。' if score else ''),
                    'evidence':'已交卷标识与同一试卷成绩页', 'score': score[1] if score else None,
                    'total':score[2] if score else None}
        except (ValueError, BrowserError):
            return None


@contextmanager
def connect_agent_browser(endpoint, source, home, cancelled):
    """Disconnect only our CDP client on exit; never close the user's browser."""
    with sync_playwright() as engine:
        browser = engine.chromium.connect_over_cdp(endpoint, timeout=10000)
        matches = [p for c in browser.contexts for p in c.pages if p.url == source]
        if len(matches) != 1:
            raise ValueError('没有找到唯一的内置学习页面，请重新连接浏览器。')
        yield AgentBrowser(matches[0], home, cancelled)
