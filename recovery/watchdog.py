import multiprocessing as mp
import threading
import time
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
        self.process = mp.get_context('spawn').Process(target=self.worker_target, args=(self.config,), daemon=False)
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
                page_hung = desired in ('catalog', 'scan', 'run') and now - self.store.get('page_heartbeat', now) > self.config['watchdog_timeout_seconds']
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
