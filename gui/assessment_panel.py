"""Consent, chat, and task state for the learning browser's Agent console."""
from collections import Counter
import threading
import uuid

from core.assessment_model import AssessmentModelError, review_questions, summarize_inventory
from core.assessment_agent import run_agent
from core.assessment_credentials import clear_key, load_key, save_key
from core.config import app_root
from gui.assessment_tools import AGENT_PROMPT, BrowserAgentTools


MODELS = ('deepseek-flash', 'deepseek-v4-pro')
EFFORTS = ('none', 'low', 'high', 'max')


class AssessmentPanel:
    def __init__(self, owner):
        self.owner = owner
        self.key = ''
        self.account = None
        self.scan_id = None
        self.summary = ''
        self.operation = None
        self.scope = object()
        self.capture = None
        self.answers = []
        self.message = ''
        self._credentials_loaded = False
        self.history = []
        self.steps = []
        self.fill_receipt = {}
        self.entry_id = None
        self.entry_options = None
        self.agent_uses_browser = False

    @property
    def owns_browser(self):
        return self.operation == 'agent' and self.agent_uses_browser

    def reset(self, *, clear_chat=False):
        self.scope = object()
        self.scan_id = None
        self.summary = ''
        self.capture = None
        self.answers = []
        self.message = ''
        self.entry_id = None
        self.entry_options = None
        self.steps = []
        if clear_chat:
            self.history = []
            self.fill_receipt = {}

    def _start(self, operation, work, on_success):
        scope = self.scope
        self.operation = operation
        self.message = ''

        def run():
            try:
                result = work()
                error = ''
            except (ValueError, AssessmentModelError) as exc:
                error = str(exc)
            except Exception:
                error = '操作暂未完成，请检查连接后重试。'
            with self.owner._lock:
                self.operation = None
                if self.scope is scope and not self.owner.closing:
                    if error:
                        self.message = error
                    else:
                        on_success(result)

        threading.Thread(target=run, daemon=True, name='RainAssessment').start()

    def snapshot(self, report, account, desired, available, entry=None):
        if self.account is not None and account != self.account:
            self.reset(clear_chat=True)
        self.account = account
        report = report if report.get('account') == account else {}
        tasks = report.get('tasks', [])
        stats = {
            '课程数': report.get('courses', 0), '已扫描课程': report.get('finished', 0),
            '读取失败课程': len(report.get('failed', [])), '按类型': dict(Counter(item['kind'] for item in tasks)),
            '按状态': dict(Counter(item['state'] for item in tasks)),
        }
        if (available and report.get('phase') == 'done' and self.scan_id and self.scan_id == report.get('id')
                and self.key and not self.operation):
            self.scan_id = None
            key = self.key
            self._start('summary', lambda: summarize_inventory(stats, key), lambda text: setattr(self, 'summary', text))
        entry = entry or {}
        current_entry = entry if self.entry_id and self.entry_id == entry.get('id') else {}
        browser = self.owner.embedded_browser
        if (current_entry.get('phase') == 'opened' and desired in ('idle', 'paused') and available
                and not self.operation and not self.owner._busy() and browser.interactive
                and browser.rect is not None and browser.snapshot()['phase'] == 'ready'):
            self.entry_id = None
            if self.key and self.entry_options and browser.document_digest() == current_entry['document']:
                self._agent('检查并填写当前已授权的作业或测验题页。先读取题目，必要时观察并建立题目分组。'
                            '把共享阅读材料和题干结合起来，只填写可核验的答案。交卷由使用者操作。',
                            self.entry_options, fill=True, read=True)
            else:
                self.message = '打开后网页发生变化，请核对页面，再点「填写当前题页」。'

        phase = report.get('phase', 'idle')
        if phase in ('queued', 'running') and desired != 'assessment_scan':
            phase = 'cancelled'
        questions, warnings, _ = self.capture or ([], [], None)
        issues = [f"第 {item['id']} 题 · {'、'.join(item['issues'])}" for item in questions if item['issues']]
        return {
            'available': available, 'has_key': bool(self.key), 'operation': self.operation,
            'phase': phase, 'message': self.message, 'scan_message': report.get('message', ''),
            'stats': stats, 'tasks': tasks, 'failed': report.get('failed', []), 'summary': self.summary,
            'captured': len(questions), 'readable': len(questions) - len(issues), 'issues': issues,
            'warnings': warnings, 'answers': self.answers,
            'history': self.history, 'steps': self.steps, 'fill_receipt': self.fill_receipt,
            'entry': {'phase': current_entry.get('phase'), 'message': current_entry.get('message', '')},
        }

    def _credential_root(self):
        config = self.owner.base_config or {}
        return config.get('root') or app_root()

    def _options(self, payload):
        model = payload.get('model')
        effort = payload.get('effort')
        online = payload.get('web_search')
        if model not in MODELS or effort not in EFFORTS or type(online) is not bool or (online and model != MODELS[1]):
            raise ValueError('请选择有效模型和推理强度。联网搜索需要 Pro。')
        return {'model': model, 'effort': effort, 'web_search': online}

    def _agent(self, text, options, *, fill=False, read=False):
        if not self.key:
            raise ValueError('请先保存 DeepSeek API Key。')
        owner, scope, key = self.owner, self.scope, self.key
        self.agent_uses_browser = read
        self.steps = []
        self.fill_receipt = {}
        self.history = [*self.history[-18:], {'role': 'user', 'text': text}]
        messages = [{'role': 'system', 'content': AGENT_PROMPT}]
        if fill:
            messages.append({'role': 'user', 'content': text})
        else:
            messages.extend({'role': item['role'], 'content': item['text'][:6000]} for item in self.history)

        def cancelled():
            return scope is not self.scope or owner.closing

        def event(step):
            with owner._lock:
                if not cancelled() and len(self.steps) < 24:
                    self.steps = [*self.steps, step]

        def receipt(result):
            with owner._lock:
                if not cancelled() and ('filled' in result or 'filled' not in self.fill_receipt):
                    self.fill_receipt = result

        def run():
            if read:
                browser_tools = BrowserAgentTools(owner.embedded_browser, key, fill=fill, cancelled=cancelled,
                                                  on_event=event, on_receipt=receipt)
                return run_agent(key, messages, **options, tools=browser_tools.specs(), execute=browser_tools.execute,
                                 cancelled=browser_tools.stopped)
            return run_agent(key, messages, **options, tools=[], execute=lambda *_: {}, cancelled=cancelled)

        def remember(result):
            self.history = [*self.history, {'role': 'assistant', **result, 'steps': self.steps,
                                           'receipt': self.fill_receipt}]
            self.fill_receipt = {}

        self._start('agent', run, remember)

    def action(self, name, payload):
        owner = self.owner
        if not owner.embedded_browser or owner.demo:
            raise ValueError('请在内置浏览器中打开 Agent。')
        if name == 'assessment_console':
            if payload:
                raise ValueError('控制台参数无效。')
            if not self._credentials_loaded:
                self._credentials_loaded = True
                try:
                    self.key = load_key(self._credential_root())
                except ValueError as exc:
                    self.message = str(exc)
            return {'ok': True}
        if name == 'assessment_save_key':
            if set(payload) != {'api_key'} or self.operation:
                raise ValueError('请结束当前操作后再修改密钥。')
            save_key(self._credential_root(), payload['api_key'])
            self.key = payload['api_key'].strip()
            self._credentials_loaded = True
            self.message = '已保存到当前 Windows 用户。'
            return {'ok': True}
        if name == 'assessment_clear_key':
            if payload or self.operation:
                raise ValueError('请结束当前操作后再移除密钥。')
            clear_key(self._credential_root())
            self.key = ''
            self.message = '已移除本机密钥。'
            return {'ok': True}
        if name == 'assessment_cancel':
            if payload:
                raise ValueError('扫描参数无效')
            self.reset()
            if owner.store.get('desired') in ('assessment_scan', 'assessment_open'):
                owner.store.set('desired', 'paused')
            if self.operation:
                self.message = '已停止后续操作，正在等待当前请求结束。'
            return {'ok': True}
        if name == 'assessment_clear_chat':
            if payload or self.operation:
                raise ValueError('请结束当前操作后再清空对话。')
            self.history = []
            self.steps = []
            self.fill_receipt = {}
            return {'ok': True}
        if self.operation:
            raise ValueError('请等待当前操作结束')
        if name == 'assessment_chat':
            if set(payload) != {'text', 'model', 'effort', 'web_search', 'read_page'} or type(payload['read_page']) is not bool:
                raise ValueError('聊天参数无效。')
            text = payload['text']
            if not isinstance(text, str) or not text.strip() or len(text) > 6000:
                raise ValueError('请输入 1 至 6000 字符。')
            if payload['read_page'] and (owner._busy() or not owner.watchdog or not owner._alive()):
                raise ValueError('请先结束当前浏览器操作，再允许 Agent 读取网页。')
            self._agent(text.strip(), self._options(payload), read=payload['read_page'])
            return {'ok': True}
        if owner._busy() or not owner.watchdog or not owner._alive():
            raise ValueError('请先连接内置浏览器并等待当前任务结束。')
        if name == 'assessment_run_current':
            if set(payload) != {'model', 'effort', 'web_search'}:
                raise ValueError('填写参数无效。')
            self._agent('观察并填写当前这页已批准的题目。只填写工具复核通过的题目，'
                        '若需要开始计时或交卷，停下并告知使用者。', self._options(payload), fill=True, read=True)
            return {'ok': True}
        if name == 'assessment_select_task':
            if set(payload) != {'id', 'model', 'effort', 'web_search'} or not self.key:
                raise ValueError('请先配置密钥并扫描任务。')
            account = owner.store.get('account', {}).get('id')
            report = owner.store.get('assessment_inventory', {})
            task = next((item for item in report.get('tasks', []) if item['id'] == payload['id']
                         and item['state'] not in ('completed', 'locked')), None)
            if (not account or report.get('account') != account
                    or report.get('phase') not in ('done', 'cancelled', 'error') or not task):
                raise ValueError('此任务需要重新扫描，或已经完成或尚未开放。')
            self.entry_options = self._options(payload)
            self.entry_id = uuid.uuid4().hex
            self.message = ''
            owner.store.set('assessment_entry', {'id': self.entry_id, 'phase': 'queued',
                'account': account, 'task_id': task['id'], 'resume': owner.store.get('desired', 'idle'),
                'start_authorized': True, 'message': f"正在进入「{task['title']}」，题页打开后会自动作答…"})
            owner.store.set('desired', 'assessment_open')
            return {'ok': True}
        if name == 'assessment_scan':
            if payload and set(payload) != {'api_key'}:
                raise ValueError('扫描参数无效')
            key = payload.get('api_key', '')
            if not isinstance(key, str) or len(key) > 512 or any(not 33 <= ord(c) <= 126 for c in key):
                raise ValueError('DeepSeek API Key 格式无效')
            if not key and not self.key:
                raise ValueError('请先填写 DeepSeek API Key')
            account = owner.store.get('account', {}).get('id')
            if not account:
                raise ValueError('请先登录雨课堂')
            self.reset()
            self.key = key or self.key
            self.account = account
            self.scan_id = uuid.uuid4().hex
            owner.store.set('assessment_inventory', {'id': self.scan_id, 'account': account, 'phase': 'queued',
                'resume': owner.store.get('desired', 'idle')})
            owner.store.set('desired', 'assessment_scan')
            return {'ok': True}
        if payload:
            raise ValueError('题页操作参数无效')
        browser = owner.embedded_browser
        if name == 'assessment_capture':
            self.capture = None
            self.answers = []
            self._start('capture', browser.read_assessment_page, lambda result: setattr(self, 'capture', result))
        elif name == 'assessment_review':
            if not self.capture or not self.key or all(item['issues'] for item in self.capture[0]):
                raise ValueError('请先开启答题助手，再读取当前题页')
            capture, key, scope = self.capture, self.key, self.scope
            self.answers = []

            def review():
                if browser.read_assessment_page() != capture:
                    raise ValueError('题页已变化，请重新读取后确认。')
                questions = capture[0]

                def cancelled():
                    return self.scope is not scope or browser.navigation_revision != capture[2]

                results = review_questions(questions, key, cancelled=cancelled)
                if cancelled():
                    raise ValueError('复核期间题页已改变，本次结果已丢弃。')
                for question, result in zip(questions, results, strict=True):
                    options = {item['id']: item['text'] for item in question['options']}
                    answer = result['answer']
                    if answer is not None and options:
                        answer = ' / '.join(options[item] for item in (answer if isinstance(answer, list) else [answer]))
                    result.update(answer=answer, stem=question['stem'][:220])
                return results

            self._start('review', review, lambda results: setattr(self, 'answers', results))
        else:
            raise ValueError('未知答题操作')
        return {'ok': True}
