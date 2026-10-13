import multiprocessing as mp
import threading
import time
import uuid
from queue import Full
from collections import deque
import psutil
from core.logging_setup import make_logger, record
from core.store import Store
from worker import worker_entry


def kill_owned(owned):
    """仅清理已记录 PID 且创建时间一致的本程序子进程，绝不全局结束 Edge。"""
    targets = []
    for pid, created in owned.items():
        try:
            process = psutil.Process(pid)
            if abs(process.create_time() - created) < .01:
                targets.append(process)
                process.terminate()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    _, alive = psutil.wait_procs(targets, timeout=3)
    for process in alive:
        try:
            process.kill()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass


class Watchdog(threading.Thread):
    def __init__(self, config, worker_target=worker_entry):
        super().__init__(daemon=True, name='RainClassroomWatchdog')
        self.config = config
        self.store = Store(config['root'])
        self.logger = make_logger(config['root'], 'watchdog')
        self.stop_event = threading.Event()
        self.process = None
        self.owned = {}
        self.restarts = deque()
        self.worker_target = worker_target
        # 用户输入文本只经过易失的进程队列，不写数据库/文件/日志。
        # 每次启动换一条队列和 generation，重启不会重放旧文字。
        self.typing_queue = None
        self.typing_generation = ''
        self.typing_lock = threading.Lock()

    def submit_typing(self, command):
        with self.typing_lock:
            if (self.stop_event.is_set() or self.typing_queue is None or not self.process
                    or not self.process.is_alive()
                    or command.get('generation') != self.typing_generation):
                return False
            try:
                self.typing_queue.put_nowait(command)
                return True
            except (Full, ValueError, OSError):
                return False

    def _discard_typing(self):
        with self.typing_lock:
            queue, self.typing_queue = self.typing_queue, None
            self.typing_generation = ''
            if queue is not None:
                # 不 join feeder：退出或崩溃时旧消息必须丢弃，不能挂住桌面关闭。
                queue.cancel_join_thread()
                queue.close()
        self.store.set('typing_status', {'phase': 'idle', 'target': None, 'sent': 0,
                                        'total': 0, 'speed': 20, 'message': '请重新指定网页输入框'})

    def remember_children(self):
        if not self.process:
            return
        try:
            root = psutil.Process(self.process.pid)
            for child in [root, *root.children(recursive=True)]:
                self.owned[child.pid] = child.create_time()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass

    def cleanup(self):
        self._discard_typing()
        self.remember_children()
        kill_owned(self.owned)
        if self.process:
            self.process.join(timeout=3)
            if not self.process.is_alive():
                self.process.close()
        self.process = None
        self.owned = {}

    def launch(self):
        now = time.time()
        self.store.set('heartbeat', now)
        self.store.set('page_heartbeat', now)
        context = mp.get_context('spawn')
        with self.typing_lock:
            self.typing_queue = context.Queue(maxsize=16)
            self.typing_generation = uuid.uuid4().hex
            worker_config = dict(self.config, typing_commands=self.typing_queue,
                                 typing_generation=self.typing_generation)
        self.store.set('typing_status', {'phase': 'idle', 'target': None, 'sent': 0,
            'total': 0, 'speed': 20, 'generation': self.typing_generation,
            'message': '请先指定受管理 Edge 内的输入框'})
        self.process = context.Process(target=self.worker_target, args=(worker_config,), daemon=False)
        self.process.start()
        self.remember_children()
        self.store.set('worker_pid', self.process.pid)

    def run(self):
        try:
            self.launch()
            while not self.stop_event.wait(1):
                self.remember_children()
                now = time.time()
                desired = self.store.get('desired', 'idle')
                if desired == 'shutdown':
                    break
                dead = not self.process.is_alive()
                hung = now - self.store.get('heartbeat', now) > self.config['watchdog_timeout_seconds']
                typing_active = self.store.get('typing_status', {}).get('phase') in ('selecting', 'ready', 'running', 'paused')
                page_hung = (desired in ('catalog', 'scan', 'run') or typing_active) and now - self.store.get('page_heartbeat', now) > self.config['watchdog_timeout_seconds']
                if not (dead or hung or page_hung):
                    continue
                self.cleanup()
                self.store.recover()
                while self.restarts and now - self.restarts[0] > 3600:
                    self.restarts.popleft()
                if len(self.restarts) >= self.config['max_restarts_per_hour']:
                    self.store.set('desired', 'paused')
                    self.store.set('worker_status', '频繁崩溃：已停止自动重启，请检查日志后重开助手')
                    self.store.set('scan_progress', {**self.store.get('scan_progress', {}), 'status': 'failed',
                                                    'message': '浏览器反复启动失败，扫描已停止，请检查日志'})
                    record(self.store, self.logger, '守护停止', '一小时内重启次数达到上限')
                    return
                self.restarts.append(now)
                record(self.store, self.logger, '重启 Edge', '工作进程退出或心跳超时；任务状态已持久化')
                if self.stop_event.wait(min(30, 2 ** len(self.restarts))):
                    break
                self.launch()
        except Exception as exc:
            self.store.set('worker_status', f'守护错误：{type(exc).__name__}')
            self.store.set('scan_progress', {**self.store.get('scan_progress', {}), 'status': 'failed',
                                            'message': f'守护进程异常，扫描已停止：{type(exc).__name__}'})
            record(self.store, self.logger, '守护错误', type(exc).__name__)
        finally:
            # 正常退出先让工作进程关闭 context，确保登录数据落盘。
            if self.stop_event.is_set() and self.process and self.process.is_alive():
                self.process.join(timeout=12)
            self.cleanup()

    def stop(self):
        self.store.set('desired', 'shutdown')
        self.stop_event.set()
