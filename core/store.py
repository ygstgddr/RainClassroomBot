"""SQLite WAL：GUI、守护线程与工作进程通过短事务交换状态。

完成记录必须带服务器证据。账号作为任务主键的一部分，避免换号串数据。
"""
import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path


class Store:
    def __init__(self, root):
        self.path = Path(root) / 'data' / 'state.sqlite3'
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.db() as db:
            db.executescript('''
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS tasks(
                    key TEXT PRIMARY KEY, account TEXT NOT NULL, classroom_id TEXT NOT NULL,
                    leaf_id TEXT NOT NULL, course TEXT NOT NULL, title TEXT NOT NULL,
                    kind TEXT NOT NULL, leaf_type INTEGER NOT NULL, state TEXT NOT NULL,
                    progress REAL, deadline REAL, duration REAL, attempts INTEGER DEFAULT 0,
                    note TEXT DEFAULT '', evidence TEXT DEFAULT '', updated REAL NOT NULL,
                    active INTEGER DEFAULT 1);
                CREATE TABLE IF NOT EXISTS events(
                    id INTEGER PRIMARY KEY AUTOINCREMENT, time REAL, course TEXT,
                    task TEXT, status TEXT, reason TEXT);
            ''')

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA busy_timeout=10000')
        try:
            with db:
                yield db
        finally:
            db.close()

    def set(self, key, value):
        with self.db() as db:
            # 使用数据库内的条件比较；多个进程修改控制状态时不能依赖内存缓存。
            # 无变化的状态不生成 WAL 写入，降低 GUI/守护进程同时轮询时的磁盘负担。
            db.execute('INSERT INTO meta VALUES(?,?) ON CONFLICT(key) DO UPDATE '
                       'SET value=excluded.value WHERE meta.value<>excluded.value',
                       (key, json.dumps(value, ensure_ascii=False)))

    def get(self, key, default=None):
        with self.db() as db:
            row = db.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def merge_course(self, account, classroom_id, tasks):
        with self.db() as db:
            db.execute('UPDATE tasks SET active=0 WHERE account=? AND classroom_id=?', (account, classroom_id))
            for task in tasks:
                task = dict(task, account=account, updated=time.time(), active=1)
                columns = list(task)
                update = ','.join(f'{c}=excluded.{c}' for c in columns if c != 'key')
                db.execute(f"INSERT INTO tasks({','.join(columns)}) VALUES({','.join('?' for _ in columns)}) "
                           f'ON CONFLICT(key) DO UPDATE SET {update}', tuple(task.values()))

    def catalog(self, account=None):
        account = account or self.get('account', {}).get('id', '')
        return self.get('course_catalog:' + account, [])

    def selected_courses(self, account=None):
        account = account or self.get('account', {}).get('id', '')
        # 没选过和主动清空都表示不扫描，绝不回退到所有课程。
        return self.get('selected_courses:' + account, [])

    def select_courses(self, ids, account=None):
        account = account or self.get('account', {}).get('id', '')
        self.set('selected_courses:' + account, sorted(set(str(cid) for cid in ids)))

    def scoped_rows(self, account=None):
        selected = set(self.selected_courses(account))
        return [t for t in self.rows(account) if t['classroom_id'] in selected]

    def scope_scanned(self, account=None):
        account = account or self.get('account', {}).get('id', '')
        selected = self.selected_courses(account)
        return bool(selected) and self.get('scanned_account') == account and self.get('scanned_courses') == selected

    def rows(self, account=None):
        account = account or self.get('account', {}).get('id', '')
        with self.db() as db:
            rows = db.execute('SELECT * FROM tasks WHERE account=? AND active=1 ORDER BY '
                              'CASE WHEN deadline>0 THEN deadline ELSE 99999999999 END, '
                              'CASE kind WHEN "article" THEN 0 WHEN "video" THEN 1 ELSE 2 END, course, leaf_id',
                              (account,)).fetchall()
        return [dict(row) for row in rows]

    def update(self, key, **changes):
        allowed = {'state', 'progress', 'duration', 'attempts', 'note', 'evidence'}
        if not changes.keys() <= allowed:
            raise ValueError('未知任务字段')
        if changes.get('state') == 'completed' and not changes.get('evidence'):
            raise ValueError('完成状态必须附带服务器证据')
        if not changes:
            return
        # IS NOT 同时正确处理 NULL，updated 仅在实际字段变化时更新。
        # 持续播放时 duration 通常不变，不应每次媒体检查都提交一次写事务。
        conditions = ' OR '.join(k + ' IS NOT ?' for k in changes)
        compared = tuple(changes.values())
        changes['updated'] = time.time()
        with self.db() as db:
            db.execute(f"UPDATE tasks SET {','.join(k+'=?' for k in changes)} WHERE key=? "
                       f'AND ({conditions})', (*changes.values(), key, *compared))

    def recover(self):
        with self.db() as db:
            db.execute("UPDATE tasks SET state='pending', note='进程恢复：执行前重新查询服务器' WHERE state='running'")

    def event(self, status, reason='', task=None):
        task = task or {}
        with self.db() as db:
            db.execute('INSERT INTO events(time,course,task,status,reason) VALUES(?,?,?,?,?)',
                       (time.time(), task.get('course', ''), task.get('title', ''), status, reason))
            # GUI 事件窗口有界；完整历史保留在轮转文件日志。
            db.execute('DELETE FROM events WHERE id < (SELECT COALESCE(MAX(id),0)-10000 FROM events)')

    def events(self, limit=100):
        with self.db() as db:
            return [dict(x) for x in db.execute('SELECT * FROM events ORDER BY id DESC LIMIT ?', (limit,))]
