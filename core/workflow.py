"""一次操作贯穿登录、扫描和执行；登录等待的操作可跨进程恢复。

desired 是唯一控制入口。以比较后更新方式切换它，避免网络返回后覆盖
用户刚按下的暂停、关闭或切站操作。workflow 为界面公开的阶段提示。
"""
import json
import time

from browser.visibility import visible_security_challenge
from core.errors import AdaptationRequired, Interrupted, LoginRequired, RetryableError
from core.logging_setup import record
from task.scanner import scan
from task.scheduler import run_queue


class Workflow:
    def __init__(self, adapter, browser, store, config, logger):
        self.adapter, self.browser, self.store = adapter, browser, store
        self.config, self.logger = config, logger
        self.expected_account = store.get('account', {}).get('id')

    def show(self, phase, message, action=None):
        self.store.set('workflow', dict(phase=phase, message=message,
                                       action=action, updated=time.time()))
        self.store.set('worker_status', message)

    def transition(self, expected, target):
        """返回中的旧请求不能代替用户的新操作。"""
        with self.store.db() as db:
            result = db.execute('UPDATE meta SET value=? WHERE key=? AND value=?',
                                (json.dumps(target), 'desired', json.dumps(expected)))
            return result.rowcount == 1

    def check(self, action):
        if self.store.get('desired') != action:
            raise Interrupted()

    def clear_login(self):
        if self.store.get('login_resume'):
            self.store.set('login_resume', None)

    def account_changed(self, account, action):
        self.store.set('account', account)
        self.store.set('scanned_account', None)
        self.store.set('run_mode', 'start')
        self.store.set('blocked_task', None)
        self.store.set('current', {})
        self.clear_login()
        # 只更新新账号目录；已有选课记录也不能继承旧账号的运行意图。
        if self.transition(action, 'catalog'):
            self.show('account_changed', '账号已切换，正在更新课程；请确认选课后再开始', 'catalog')
            record(self.store, self.logger, '账号切换', '已停止旧账号操作，只更新新账号课程目录')

    async def perform(self, action):
        """开始扫描最新任务；继续可复用完整扫描，执行仍查询服务器。"""
        self.clear_login()
        self.expected_account = self.store.get('account', {}).get('id')
        self.show('checking_login', '正在检查登录状态…', action)
        self.adapter.account_id = None
        account = await self.adapter.account()
        self.check(action)
        if self.expected_account and self.expected_account != account['id'] and action != 'catalog':
            self.account_changed(account, action)
            return
        self.expected_account = account['id']
        self.store.set('account', account)

        if action == 'catalog':
            self.show('catalog', '正在载入课程和开课时间…', action)
            result = await scan(self.adapter, self.store, self.config, self.logger, catalog_only=True)
            self.check(action)
            target = 'paused' if result['status'] == 'failed' else 'idle'
            if self.transition(action, target):
                message = ('课程时间未能完整读取，请查看扫描异常' if result['status'] != 'completed'
                           else '课程已更新，选好课程后即可开始')
                self.show('error' if target == 'paused' else 'ready', message)
            return

        if not self.store.selected_courses(account['id']):
            if self.transition(action, 'idle'):
                self.show('ready', '请先选择需要处理的课程')
            return

        resume = (action == 'run' and self.store.get('run_mode', 'start') == 'resume'
                  and self.store.scope_scanned(account['id'])
                  and self.store.get('scan_progress', {}).get('status') == 'completed')
        if not resume:
            self.show('scanning', '正在检查所选课程的最新任务…', action)
            result = await scan(self.adapter, self.store, self.config, self.logger)
            self.check(action)
            if result['status'] != 'completed':
                self.store.set('scanned_account', None)
                if self.transition(action, 'paused'):
                    self.show('error', '部分课程未能扫描成功，已暂停；请查看扫描异常后重试')
                    record(self.store, self.logger, '流程暂停', '本轮扫描不完整，未开始执行任务')
                return
        if not self.store.scope_scanned(account['id']):
            if self.transition(action, 'paused'):
                self.show('paused', '选课已变化，请重新开始以更新任务')
            return
        if action == 'scan':
            if self.transition(action, 'idle'):
                self.show('ready', '任务已更新，可以直接开始')
            return
        # 扫描已完整完成；暂停或浏览器崩溃后复用范围，不必重读整批目录。
        # 新一轮“开始”由界面显式写入 start，改选或失败扫描仍会走上面的扫描。
        self.store.set('run_mode', 'resume')
        self.show('running', '正在处理所选任务，完成状态以服务器记录为准', action)
        summary = await run_queue(self.adapter, self.browser, self.store, self.config, self.logger)
        desired = self.store.get('desired')
        if desired == 'idle':
            rows = self.store.scoped_rows()
            remaining = sum(t['state'] != 'completed' for t in rows)
            message = ((summary or {}).get('message') or
                       ('所选课程暂未发现任务' if not rows else
                        f'仍有 {remaining} 项未完成，请查看任务状态'
                        if remaining else '本轮任务已处理，服务器已确认完成'))
            self.show('ready', message)
        elif desired == 'paused':
            self.show('paused', '已暂停，可查看任务状态后继续')

    def request_login(self, action, message):
        """保存明确的原操作；不会覆盖已经生效的暂停或关闭。"""
        if not self.transition(action, 'login'):
            return False
        self.store.set('login_resume', {'action': action, 'account': self.expected_account})
        self.store.set('login_message', message)
        self.show('waiting_login', '请在 Edge 完成登录；助手会自动识别并继续', action)
        record(self.store, self.logger, '等待登录', message)
        return True

    async def poll_login(self):
        """只读账号，不刷新登录页，不重复打开浏览器。"""
        self.check('login')
        intent = self.store.get('login_resume') or {'action': 'catalog', 'account': self.expected_account}
        try:
            self.adapter.account_id = None
            account = await self.adapter.account()
            self.check('login')
            # 账号接口有效不代表网页中真实可见的验证已经消失。
            for frame in getattr(self.browser.page, 'frames', ()):
                if await visible_security_challenge(frame):
                    self.show('waiting_login', '请在 Edge 完成页面验证；完成后会自动继续', intent['action'])
                    return False
        except LoginRequired:
            return False
        except (RetryableError, AdaptationRequired) as exc:
            self.show('waiting_login', f'等待登录：{exc}', intent['action'])
            return False
        self.check('login')
        if intent.get('account') and intent['account'] != account['id']:
            self.account_changed(account, 'login')
            return True
        self.store.set('account', account)
        self.expected_account = account['id']
        action = intent.get('action') if intent.get('action') in ('catalog', 'scan', 'run') else 'catalog'
        if self.transition('login', action):
            self.clear_login()
            self.show('checking_login', '登录已恢复，正在继续…', action)
            record(self.store, self.logger, '登录恢复', '已自动恢复原操作')
            return True
        return False
