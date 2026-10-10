import os
import time
import tkinter as tk
from tkinter import ttk, messagebox
from pathlib import Path
from core.courses import course_state, date_text
from core.task_types import type_label
from core.sites import PRESETS, normalize_site, read_sites, save_site, site_config, prepare_site


STATES = {'pending': '未完成', 'running': '执行中', 'completed': '服务器已确认',
          'manual': '人工处理', 'locked': '未开放', 'needs_adapter': '需适配', 'failed': '失败'}
KINDS = {'video': '视频', 'article': '图文', 'manual': '其他任务'}


class Interface:
    def __init__(self, root, store, watchdog=None, demo=False, base_config=None, watchdog_factory=None):
        self.root, self.store, self.watchdog, self.demo = root, store, watchdog, demo
        self.closing = False
        self.base_config = base_config
        self.watchdog_factory = watchdog_factory
        self.switching = False
        self.active_site, history = read_sites(base_config) if base_config else (PRESETS[0], list(PRESETS))
        self.buttons = {}
        self.course_buttons = []
        self.progress_animating = False
        root.title('雨课堂 · 智能任务管理助手' + ('  [演示数据]' if demo else ''))
        root.geometry('1160x820')
        root.minsize(900, 660)
        root.configure(bg='#f2f5f9')
        style = ttk.Style(root)
        style.theme_use('clam')
        style.configure('.', font=('Microsoft YaHei UI', 10))
        style.configure('TFrame', background='#f2f5f9')
        style.configure('TLabel', background='#f2f5f9', foreground='#22334a')
        style.configure('Title.TLabel', font=('Microsoft YaHei UI', 22, 'bold'))
        style.configure('Sub.TLabel', foreground='#53677f')
        style.configure('TButton', padding=(12, 8))
        style.configure('TButton', background='#ffffff', bordercolor='#d4deeb')
        style.map('TButton', background=[('active', '#e0ebfa')])
        style.configure('Accent.TButton', background='#245eb7', foreground='white')
        style.map('Accent.TButton', background=[('active', '#1b4b95')])
        style.configure('TNotebook.Tab', padding=(12, 7), background='#e7edf6')
        style.map('TNotebook.Tab', background=[('selected', 'white')])
        style.configure('Treeview', rowheight=34, background='white', fieldbackground='white', borderwidth=0)
        style.configure('Treeview.Heading', font=('Microsoft YaHei UI', 10, 'bold'), padding=(8, 10), background='#e9eff7')
        style.map('Treeview', background=[('selected', '#d8e9ff')], foreground=[('selected', '#17385f')])
        frame = ttk.Frame(root, padding=24)
        frame.pack(fill='both', expand=True)
        ttk.Label(frame, text='雨课堂任务管理', style='Title.TLabel').pack(anchor='w')
        ttk.Label(frame, text='持久登录  /  实时进度  /  中断恢复', style='Sub.TLabel').pack(anchor='w', pady=(4, 16))
        sites = ttk.Frame(frame)
        sites.pack(fill='x', pady=(0, 12))
        ttk.Label(sites, text='雨课堂网站：').pack(side='left')
        self.site_address = tk.StringVar(value=self.active_site)
        self.site_entry = ttk.Combobox(sites, textvariable=self.site_address, values=history)
        self.site_entry.pack(side='left', fill='x', expand=True, padx=(0, 8))
        self.site_entry.bind('<Return>', lambda event: self.switch_site())
        self.site_button = ttk.Button(sites, text='切换网站', command=self.switch_site)
        self.site_button.pack(side='left')
        self.site_hint = ttk.Label(frame, text=f'当前网站：{self.active_site}  · 各站登录和任务分别保存', style='Sub.TLabel')
        self.site_hint.pack(anchor='w', pady=(0, 10))
        self.summary = ttk.Label(frame, text='账号：等待登录', font=('Microsoft YaHei UI', 11))
        self.summary.pack(anchor='w', pady=(0, 16))
        toolbar = ttk.Frame(frame)
        toolbar.pack(fill='x', pady=(0, 16))
        for label, command in [('开始任务', lambda: self.command('run')), ('暂停', lambda: self.command('paused')),
                               ('继续', lambda: self.command('run')), ('重新扫描', lambda: self.command('scan')),
                               ('重试选中任务', self.retry_selected), ('查看日志', self.open_logs)]:
            button = ttk.Button(toolbar, text=label, command=command,
                                style='Accent.TButton' if label == '开始任务' else 'TButton')
            button.pack(side='left', padx=(0, 7))
            self.buttons[label] = button
        self.status = ttk.Label(frame, text='演示模式 · 不连接真实课程' if demo else '准备中', wraplength=1080)
        self.status.pack(anchor='w', pady=(0, 8))
        self.scan_label = ttk.Label(frame, text='尚未扫描', wraplength=1080, style='Sub.TLabel')
        self.scan_label.pack(anchor='w', pady=(0, 5))
        self.scan_bar = ttk.Progressbar(frame, mode='determinate', maximum=100)
        self.scan_bar.pack(fill='x', pady=(0, 12))
        self.only_unfinished = tk.BooleanVar(value=True)
        ttk.Checkbutton(frame, text='只显示未完成 / 待确认任务', variable=self.only_unfinished).pack(anchor='w', pady=(0, 8))
        ttk.Label(frame, text='先在“课程选择”勾选课程，再重新扫描。选择自动保存；日期为北京时间，未选课程的历史记录会保留。',
                  style='Sub.TLabel', wraplength=1080).pack(side='bottom', anchor='w', pady=(14, 0))
        tabs = ttk.Notebook(frame)
        self.tabs = tabs
        tabs.pack(fill='both', expand=True)
        self.course_tab = ttk.Frame(tabs)
        tabs.add(self.course_tab, text='  课程选择  ')
        self.build_course_panel()
        task_tab, warning_tab, logs_tab = ttk.Frame(tabs), ttk.Frame(tabs), ttk.Frame(tabs)
        tabs.add(task_tab, text='  任务队列  ')
        tabs.add(warning_tab, text='  扫描异常  ')
        tabs.add(logs_tab, text='  最近日志  ')
        columns = ('course', 'title', 'kind', 'state', 'progress', 'duration', 'note')
        self.tree = ttk.Treeview(task_tab, columns=columns, show='headings', selectmode='browse')
        for key, label, width in zip(columns, ['课程', '任务', '类型', '状态', '服务器进度', '预计时长', '说明'],
                                     [150, 190, 135, 130, 100, 90, 230]):
            self.tree.heading(key, text=label)
            self.tree.column(key, width=width, minwidth=60, stretch=key in ('title', 'note'))
        vertical = ttk.Scrollbar(task_tab, orient='vertical', command=self.tree.yview)
        horizontal = ttk.Scrollbar(task_tab, orient='horizontal', command=self.tree.xview)
        self.tree.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        task_tab.rowconfigure(0, weight=1)
        task_tab.columnconfigure(0, weight=1)
        self.tree.grid(row=0, column=0, sticky='nsew')
        vertical.grid(row=0, column=1, sticky='ns')
        horizontal.grid(row=1, column=0, sticky='ew')
        self.tree.tag_configure('completed', foreground='#228251')
        self.tree.tag_configure('failed', foreground='#bd4141')
        self.tree.tag_configure('needs_adapter', foreground='#98681a')
        self.warnings = self.text_panel(warning_tab)
        self.logs = self.text_panel(logs_tab)
        root.protocol('WM_DELETE_WINDOW', self.close)
        self.refresh()

    def build_course_panel(self):
        actions = ttk.Frame(self.course_tab, padding=(8, 8))
        actions.pack(fill='x')
        for label, callback in [('刷新课程时间', lambda: self.command('catalog')),
                                ('选择进行中课程', lambda: self.select_group('current')),
                                ('全选', lambda: self.select_group('all')),
                                ('清空选择', lambda: self.select_group('none'))]:
            button = ttk.Button(actions, text=label, command=callback)
            button.pack(side='left', padx=(0, 6))
            self.course_buttons.append(button)
        self.course_hint = ttk.Label(self.course_tab, text='正在读取课程列表…', padding=(10, 6))
        self.course_hint.pack(anchor='w')
        table = ttk.Frame(self.course_tab)
        table.pack(fill='both', expand=True)
        columns = ('selected', 'name', 'class_name', 'start', 'end', 'state', 'note')
        self.course_tree = ttk.Treeview(table, columns=columns, show='headings', selectmode='browse')
        for key, label, width in zip(columns, ['选择', '课程', '教学班 / 学期', '开课时间', '结课时间', '状态', '说明'],
                                     [55, 185, 225, 155, 155, 90, 160]):
            self.course_tree.heading(key, text=label)
            self.course_tree.column(key, width=width, minwidth=50, stretch=key in ('name', 'class_name', 'note'))
        self.course_tree.tag_configure('ended', foreground='#7a8390')
        vertical = ttk.Scrollbar(table, orient='vertical', command=self.course_tree.yview)
        horizontal = ttk.Scrollbar(table, orient='horizontal', command=self.course_tree.xview)
        self.course_tree.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        table.rowconfigure(0, weight=1)
        table.columnconfigure(0, weight=1)
        self.course_tree.grid(row=0, column=0, sticky='nsew')
        vertical.grid(row=0, column=1, sticky='ns')
        horizontal.grid(row=1, column=0, sticky='ew')
        self.course_tree.bind('<Button-1>', self.course_click)
        self.course_tree.bind('<space>', self.course_space)

    def controls_busy(self):
        return self.switching or self.store.get('worker_busy', False) or self.store.get('desired') in ('catalog', 'scan', 'run')

    def switch_site(self):
        if self.closing or self.switching:
            return
        if self.demo or self.base_config is None:
            self.status.configure(text='演示模式不切换真实网站')
            return
        try:
            url = normalize_site(self.site_address.get())
            config = site_config(self.base_config, url)
        except ValueError as exc:
            self.site_hint.configure(text=str(exc))
            return
        if url == self.active_site and self.watchdog and self.watchdog.is_alive():
            self.site_hint.configure(text='当前已是此网站；登录后点击“刷新课程时间”')
            return
        self.switching = True
        self.site_entry.configure(state='disabled')
        self.site_button.configure(state='disabled')
        self.site_hint.configure(text='正在保存当前任务并关闭原站 Edge，随后打开新网站…')
        if self.watchdog:
            self.watchdog.stop()
        self.root.after(100, lambda: self.finish_site_switch(url, config))

    def finish_site_switch(self, url, config):
        if self.closing:
            return
        if self.watchdog and self.watchdog.is_alive():
            self.root.after(200, lambda: self.finish_site_switch(url, config))
            return
        old_store, old_url = self.store, self.active_site
        try:
            from recovery.watchdog import Watchdog
            new_store = prepare_site(config)
            dog = (self.watchdog_factory or Watchdog)(config)
            save_site(self.base_config, url)
            history = read_sites(self.base_config)[1]
            dog.start()
            self.watchdog, self.store, self.active_site = dog, new_store, url
            self.site_address.set(url)
            self.site_entry.configure(values=history)
            self.site_hint.configure(text=f'当前网站：{url}  · 各站登录和任务分别保存')
            self.tabs.select(self.course_tab)
        except Exception as exc:
            self.store, self.active_site = old_store, old_url
            # 保留原站数据，允许再次切换；不把两站状态混在一起。
            self.store.set('desired', 'paused')
            self.store.set('worker_busy', False)
            self.site_hint.configure(text=f'切换失败（{type(exc).__name__}），请检查目录权限或日志后重试')
            try:
                save_site(self.base_config, old_url)
            except OSError:
                pass
        finally:
            self.switching = False
            self.site_entry.configure(state='normal')
            self.site_button.configure(state='normal')

    def course_click(self, event):
        if self.course_tree.identify_column(event.x) == '#1':
            self.toggle_course(self.course_tree.identify_row(event.y))

    def course_space(self, event=None):
        chosen = self.course_tree.selection()
        if chosen:
            self.toggle_course(chosen[0])
        return 'break'

    def save_selection(self, selected):
        if self.controls_busy():
            self.status.configure(text='请先暂停，等待当前操作停止后再修改课程选择')
            return
        self.store.select_courses(selected)
        self.render_courses()
        self.status.configure(text='课程选择已自动保存；请点击“重新扫描”更新所选课程任务')

    def toggle_course(self, cid):
        if not cid or cid not in {c['classroom_id'] for c in self.store.catalog()}:
            return
        selected = set(self.store.selected_courses())
        selected.symmetric_difference_update({cid})
        self.save_selection(selected)

    def select_group(self, mode):
        courses = self.store.catalog()
        selected = [c['classroom_id'] for c in courses
                    if mode == 'all' or (mode == 'current' and course_state(c) == '进行中')]
        self.save_selection(selected)

    def render_courses(self):
        courses = sorted(self.store.catalog(), key=lambda c: (c.get('start') or 0, c['classroom_id']), reverse=True)
        selected = set(self.store.selected_courses())
        ids = {c['classroom_id'] for c in courses}
        for cid in self.course_tree.get_children():
            if cid not in ids:
                self.course_tree.delete(cid)
        for index, course in enumerate(courses):
            cid = course['classroom_id']
            state = course_state(course)
            values = ('☑' if cid in selected else '☐', course['name'], course.get('class_name') or course.get('term', ''),
                      date_text(course.get('start')), date_text(course.get('end')), state, course.get('date_error', ''))
            if self.course_tree.exists(cid):
                self.course_tree.item(cid, values=values, tags=('ended',) if state == '已结束' else ())
                self.course_tree.move(cid, '', index)
            else:
                self.course_tree.insert('', index, iid=cid, values=values, tags=('ended',) if state == '已结束' else ())
        self.course_hint.configure(text=f'已选 {len(selected & ids)} / {len(courses)} 门 · 点击方框或按空格勾选；开课时间由新到旧排列。'
                                   ' 时间未知的课程请手动选择。')
        for button in self.course_buttons:
            button.configure(state='disabled' if self.controls_busy() else 'normal')

    def text_panel(self, parent):
        widget = tk.Text(parent, wrap='word', font=('Microsoft YaHei UI', 10), bg='white', fg='#34465e',
                         relief='flat', padx=14, pady=14)
        scrollbar = ttk.Scrollbar(parent, command=widget.yview)
        scrollbar.pack(side='right', fill='y')
        widget.configure(yscrollcommand=scrollbar.set)
        widget.pack(fill='both', expand=True)
        return widget

    def command(self, desired):
        if self.switching:
            return
        if self.demo:
            self.status.configure(text='演示模式：展示界面和样例数据。正常启动程序可使用 Edge 执行任务。')
            return
        if self.watchdog and not self.watchdog.is_alive():
            messagebox.showerror('守护进程已停止', '请查看日志并重新启动助手。')
            return
        current = self.store.get('scan_progress', {})
        if desired != 'paused' and self.controls_busy():
            self.status.configure(text='当前操作尚未停止，请先暂停并稍候')
            return
        if desired in ('scan', 'run') and not self.store.selected_courses():
            self.tabs.select(self.course_tab)
            self.status.configure(text='尚未选择课程。请先刷新课程时间并勾选需要处理的课程。')
            return
        if desired == 'run' and not self.store.scope_scanned():
            self.status.configure(text='课程选择已改变，请先重新扫描所选课程')
            return
        if desired in ('scan', 'catalog'):
            if current.get('status') in ('queued', 'running') and self.store.get('desired') == 'scan':
                return
            self.store.set('scan_progress', dict(status='queued', mode='catalog' if desired == 'catalog' else 'tasks',
                                               phase='等待读取课程时间' if desired == 'catalog' else '等待扫描启动', total=0,
                                               completed=0, succeeded=0, failed=0, tasks=0,
                                               started=time.time(), current=[]))
            self.status.configure(text='已收到重新扫描请求')
            self.render_scan_progress()
        elif desired == 'run' and current.get('status') in ('queued', 'running', 'failed', 'cancelled'):
            self.status.configure(text='请先完成重新扫描；扫描失败不表示任务已完成')
            return
        elif desired == 'paused' and current.get('status') == 'queued':
            self.store.set('scan_progress', {**current, 'status': 'cancelled', 'message': '扫描启动已取消'})
            self.render_scan_progress()
        self.store.set('desired', desired)

    def render_scan_progress(self):
        progress = self.store.get('scan_progress', {})
        phase = progress.get('status')
        scanning = phase in ('queued', 'running')
        total, completed = progress.get('total', 0), progress.get('completed', 0)
        unknown_total = scanning and total == 0
        if unknown_total and not self.progress_animating:
            self.scan_bar.configure(mode='indeterminate')
            self.scan_bar.start(12)
            self.progress_animating = True
        elif not unknown_total:
            if self.progress_animating:
                self.scan_bar.stop()
                self.progress_animating = False
            self.scan_bar.configure(mode='determinate', maximum=max(1, total),
                                    value=completed if total else (1 if phase == 'completed' else 0))
        if scanning:
            elapsed = max(0, int(time.time() - progress.get('started', time.time())))
            percent = f'{completed/total:.0%}' if total else '准备中'
            detail = '；'.join(f"{c['course']} · {c['phase']}" for c in progress.get('current', []))
            self.scan_label.configure(text=f"{progress.get('phase', '扫描中')}  {completed}/{total or '?'} 门（{percent}）"
                                           f"  已发现 {progress.get('tasks', 0)} 项  失败 {progress.get('failed', 0)} 门"
                                           f'  耗时 {elapsed} 秒' + ('\n' + detail if detail else ''))
        else:
            self.scan_label.configure(text=progress.get('message', '尚未扫描'))
        for label in ('开始任务', '继续', '重新扫描', '重试选中任务'):
            self.buttons[label].configure(state='disabled' if scanning or self.controls_busy() else 'normal')

    def retry_selected(self):
        if self.switching:
            return
        if self.demo:
            return self.command('run')
        selected = self.tree.selection()
        if not selected:
            return
        if self.store.get('desired') in ('run', 'scan'):
            messagebox.showinfo('请先暂停', '任务运行期间不能重置重试次数。请先点击暂停。')
            return
        task = next((t for t in self.store.rows() if t['key'] == selected[0]), None)
        if task and task['state'] == 'failed':
            self.store.update(task['key'], state='pending', attempts=0, note='用户重置重试预算')
        elif task and task['state'] == 'needs_adapter':
            messagebox.showinfo('需要重新扫描', '请先修正配置或检查课程页面，再重新扫描。')

    def open_logs(self):
        folder = Path(self.store.path).parents[1] / 'logs'
        folder.mkdir(exist_ok=True)
        os.startfile(folder)

    def set_text(self, widget, text):
        if widget.get('1.0', 'end-1c') != text:
            widget.configure(state='normal')
            widget.delete('1.0', 'end')
            widget.insert('1.0', text)
            widget.configure(state='disabled')

    def refresh(self):
        if self.closing:
            return
        try:
            account = self.store.get('account', {})
            tasks = self.store.scoped_rows()
            done = sum(t['state'] == 'completed' for t in tasks)
            self.summary.configure(text=f"账号：{account.get('name', '等待登录')}    课程：{self.store.get('course_count', 0)}"
                                        f"（已选 {len(self.store.selected_courses())}）    "
                                        f'任务：{len(tasks)}    已确认：{done}    待处理：{len(tasks)-done}')
            current = self.store.get('current', {})
            status = self.store.get('worker_status', '准备中')
            scan_state = self.store.get('scan_progress', {})
            if self.store.get('desired') in ('idle', 'paused') and scan_state.get('status') in ('completed', 'partial', 'failed', 'cancelled'):
                status = {'completed': '扫描已完成，可查看任务列表', 'partial': '部分课程扫描失败，请查看扫描异常',
                          'failed': '扫描失败，不能判定没有任务', 'cancelled': '扫描已暂停'}[scan_state['status']]
                if scan_state.get('mode') == 'catalog':
                    status = scan_state.get('message', status)
            if self.store.get('desired') in ('idle', 'paused') and not self.store.scope_scanned():
                status += '  |  请选择课程并重新扫描'
            if current:
                status += f"  |  {current.get('course', '')} / {current.get('title', '')} · {current.get('phase', '')}"
            if self.switching:
                self.status.configure(text='正在切换网站，等待原站浏览器安全退出…')
            elif not self.demo:
                self.status.configure(text=status)
            self.render_scan_progress()
            self.render_courses()
            visible_tasks = [t for t in tasks if t['state'] != 'completed'] if self.only_unfinished.get() else tasks
            keys = {t['key'] for t in visible_tasks}
            for iid in self.tree.get_children():
                if iid not in keys:
                    self.tree.delete(iid)
            for task in visible_tasks:
                progress = '未知' if task['progress'] is None else f"{task['progress']:.0%}"
                duration = '未知' if task['duration'] is None else f"{task['duration']/60:.1f} 分钟"
                values = (task['course'], task['title'], type_label(task),
                          STATES.get(task['state'], task['state']), progress, duration, task['note'])
                if self.tree.exists(task['key']):
                    self.tree.item(task['key'], values=values, tags=(task['state'],))
                else:
                    self.tree.insert('', 'end', iid=task['key'], values=values, tags=(task['state'],))
            errors = self.store.get('scan_errors', [])
            self.set_text(self.warnings, '\n\n'.join(f"{e['course']}\n{e['reason']}" for e in errors) or
                          '暂无已记录的扫描异常。空任务列表不代表已完成，请先成功扫描。')
            self.set_text(self.logs, '\n'.join(f"{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(e['time']))}  "
                                              f"{e['course']}  {e['task']}  {e['status']}  {e['reason']}" for e in self.store.events()))
        finally:
            self.root.after(400, self.refresh)

    def close(self):
        self.closing = True
        if self.watchdog:
            self.watchdog.stop()
            self.status.configure(text='正在保存状态并关闭助手的 Edge…')
            self.await_close()
        else:
            self.root.destroy()

    def await_close(self):
        if self.watchdog.is_alive():
            self.root.after(200, self.await_close)
        else:
            self.root.destroy()
