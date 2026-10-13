"""Native glass-style desktop with a single context-aware primary action.

The worker owns scanning/execution. This view only records the user's intent;
it never interprets a local animation or empty task list as server completion.
"""
import os
import time
import tkinter as tk
from tkinter import ttk, messagebox
from pathlib import Path
from urllib.parse import urlsplit
from core.courses import course_state, date_text
from core.task_types import type_label
from core.sites import PRESETS, normalize_site, read_sites, save_site, site_config, prepare_site
from gui.glass import GlassBackdrop, GlassButton, INK, MUTED, PANEL, BLUE


STATES = {'pending': '待完成', 'running': '执行中', 'completed': '服务器已确认',
          'manual': '人工处理', 'locked': '未开放', 'needs_adapter': '待适配', 'failed': '失败'}
KINDS = {'video': '视频', 'article': '图文', 'manual': '其他任务'}


class Interface:
    def __init__(self, root, store, watchdog=None, demo=False, base_config=None, watchdog_factory=None):
        self.root, self.store, self.watchdog, self.demo = root, store, watchdog, demo
        self.closing = False
        self.base_config, self.watchdog_factory = base_config, watchdog_factory
        self.switching = False
        self.active_site, history = read_sites(base_config) if base_config else (PRESETS[0], list(PRESETS))
        self.buttons, self.course_buttons, self.nav_buttons = {}, [], {}
        self.progress_animating = False
        self._notice_until = 0
        self._demo_running = False
        self._page = 'tasks' if store.selected_courses() else 'courses'
        self._site_dialog = None
        self._refresh_job = None
        root.title('雨课堂 · 智能任务管理助手' + ('  [演示界面]' if demo else ''))
        root.geometry('1240x850')
        root.minsize(900, 660)
        root.configure(bg='#eaf1fb')
        self._style()
        self.backdrop = GlassBackdrop(root)
        self.backdrop.pack(fill='both', expand=True)
        self._build_sidebar()
        self._build_header()
        self.workspace = tk.Frame(self.backdrop, bg=PANEL)
        self._workspace_window = self.backdrop.create_window(248, 236, anchor='nw', window=self.workspace)
        self._build_workspace()

        # Retain the established API for tests and integrations. These actions
        # appear in the More menu instead of taking a row of visible buttons.
        for text, action in [('开始任务', lambda: self.command('run')), ('暂停', lambda: self.command('paused')),
                             ('继续', lambda: self.command('run')), ('重新扫描', lambda: self.command('scan')),
                             ('重试选中任务', self.retry_selected), ('查看日志', self.open_logs)]:
            self.buttons[text] = ttk.Button(self.workspace, text=text, command=action)
        self._build_site_settings(history)
        self.backdrop.bind('<Configure>', self._layout, add='+')
        root.protocol('WM_DELETE_WINDOW', self.close)
        root.bind('<Control-f>', self.focus_search)
        root.bind('<Escape>', lambda event: self.close_site_settings())
        self.show_page(self._page)
        self.refresh()

    def _style(self):
        style = ttk.Style(self.root)
        style.theme_use('clam')
        style.configure('.', font=('Microsoft YaHei UI', 10), background=PANEL, foreground=INK)
        style.configure('TFrame', background=PANEL)
        style.configure('TLabel', background=PANEL, foreground=INK)
        style.configure('Sub.TLabel', foreground=MUTED)
        style.configure('Title.TLabel', font=('Microsoft YaHei UI', 18, 'bold'))
        style.configure('TButton', padding=(12, 8), background='white', bordercolor='#e0e7f1')
        style.map('TButton', background=[('active', '#e9f1fd')])
        style.configure('TEntry', fieldbackground='white', bordercolor='#dce5f1', lightcolor='#dce5f1',
                        darkcolor='#dce5f1', borderwidth=1, relief='flat', padding=8)
        style.map('TEntry', bordercolor=[('focus', '#91bdff')], lightcolor=[('focus', '#91bdff')],
                  darkcolor=[('focus', '#91bdff')])
        style.configure('TCombobox', fieldbackground='white', background='#eef4fb', bordercolor='#dce5f1',
                        lightcolor='#dce5f1', darkcolor='#dce5f1', borderwidth=1, relief='flat',
                        arrowcolor='#7c90aa', arrowsize=13, padding=7)
        style.map('TCombobox', fieldbackground=[('readonly', 'white')], selectbackground=[('readonly', 'white')],
                  selectforeground=[('readonly', INK)], background=[('active', '#e6effb')])
        style.configure('TCheckbutton', background=PANEL, padding=(0, 5))
        style.map('TCheckbutton', background=[('active', PANEL)])
        style.configure('Glass.TNotebook', background=PANEL, borderwidth=0)
        style.layout('Glass.TNotebook.Tab', [])
        style.configure('Treeview', rowheight=43, background=PANEL, fieldbackground=PANEL,
                        borderwidth=0, bordercolor=PANEL, lightcolor=PANEL, darkcolor=PANEL, relief='flat')
        style.configure('Treeview.Heading', font=('Microsoft YaHei UI', 9, 'bold'),
                        padding=(8, 11), background='#eef3fa', relief='flat', borderwidth=0,
                        foreground='#6a7c93')
        style.map('Treeview', background=[('selected', '#e0edff')], foreground=[('selected', '#12559c')])
        style.map('Treeview.Heading', background=[('active', '#e6edf8')])
        style.configure('Horizontal.TProgressbar', troughcolor='#e5edf8', background=BLUE,
                        borderwidth=0, bordercolor='#e5edf8', lightcolor=BLUE, darkcolor=BLUE, thickness=5)
        # Clam's native pbar keeps internal bevel padding even at borderwidth=0,
        # which clips its colored interior inside a slim track. Flat image
        # elements keep ttk's real progress semantics without that padding.
        trough = tk.PhotoImage(master=self.root, width=8, height=8)
        fill = tk.PhotoImage(master=self.root, width=8, height=8)
        trough.put('#e5edf8', to=(0, 0, 8, 8))
        fill.put(BLUE, to=(0, 0, 8, 8))
        self._progress_images = (trough, fill)
        for name, bitmap in [('Glass.Progressbar.trough', trough), ('Glass.Progressbar.pbar', fill)]:
            if name not in style.element_names():
                style.element_create(name, 'image', bitmap, border=0, sticky='nsew')
        style.layout('Glass.Horizontal.TProgressbar', [('Glass.Progressbar.trough', {'sticky': 'nswe', 'children':
                     [('Glass.Progressbar.pbar', {'side': 'left', 'sticky': 'ns'})]})])
        style.configure('TScrollbar', background='#d5dfed', troughcolor=PANEL,
                        borderwidth=0, bordercolor=PANEL, lightcolor='#d5dfed', darkcolor='#d5dfed',
                        arrowsize=10, relief='flat', gripcount=0)
        style.layout('Vertical.TScrollbar', [('Vertical.Scrollbar.trough', {'sticky': 'ns', 'children':
                     [('Vertical.Scrollbar.thumb', {'expand': '1', 'sticky': 'nswe'})]})])
        style.layout('Horizontal.TScrollbar', [('Horizontal.Scrollbar.trough', {'sticky': 'we', 'children':
                     [('Horizontal.Scrollbar.thumb', {'expand': '1', 'sticky': 'nswe'})]})])

    def _build_sidebar(self):
        c = self.backdrop
        c.create_oval(44, 46, 82, 84, fill='#007aff', outline='#8fc2ff', width=1)
        c.create_text(63, 65, text='雨', fill='white', font=('Microsoft YaHei UI', 16, 'bold'))
        c.create_text(96, 57, anchor='nw', text='雨课堂', fill=INK, font=('Microsoft YaHei UI', 15, 'bold'))
        c.create_text(46, 97, anchor='nw', text='学习，从容一点。', fill='#7890aa', font=('Microsoft YaHei UI', 9))
        for n, (key, text) in enumerate([('courses', '▦   我的课程'), ('tasks', '▷   学习任务'), ('records', '☷   运行记录')]):
            button = GlassButton(c, text=text, command=lambda p=key: self.show_page(p), width=150,
                                 height=44, quiet=True, bg='#eef4fc')
            c.create_window(38, 156+n*58, anchor='nw', window=button)
            self.nav_buttons[key] = button
        self.sidebar_tip = c.create_text(44, 369, anchor='nw', text='01  选择课程\n\n02  一键开始',
                                        fill='#8698b0', font=('Microsoft YaHei UI', 10), width=145)
        self.sidebar_line = c.create_line(40, 600, 188, 600, fill='#dce5f2')
        self.account_mark = c.create_oval(44, 628, 73, 657, fill='#e1edff', outline='')
        self.account_initial = c.create_text(58, 642, text='我', fill='#4678b5', font=('Microsoft YaHei UI', 10, 'bold'))
        self.account_label = c.create_text(83, 625, anchor='nw', text='等待登录', fill=INK,
                                           font=('Microsoft YaHei UI', 10, 'bold'), width=106)
        self.account_detail = c.create_text(44, 682, anchor='nw', text='登录状态自动保存', fill=MUTED,
                                            font=('Microsoft YaHei UI', 8), width=144)
        self.brand_note = c.create_text(44, 720, anchor='nw', text='RAINCLASSROOM\n专注 · 有序 · 可恢复', fill='#98a7bb',
                                        font=('Microsoft YaHei UI', 8))

    def _build_header(self):
        c = self.backdrop
        c.create_text(234, 30, anchor='nw', text='学习工作台', fill=INK, font=('Microsoft YaHei UI', 24, 'bold'))
        self.header_subtitle = c.create_text(236, 77, anchor='nw', text='选好课程，剩下的交给助手。',
                                            fill='#73879e', font=('Microsoft YaHei UI', 10))
        self.site_pill = GlassButton(c, text='雨课堂  ⌄', command=self.open_site_settings, width=168,
                                    height=40, bg='#eaf1fb')
        self._site_pill_window = c.create_window(1044, 42, anchor='nw', window=self.site_pill)
        self.metrics = []
        for title, note in [('已选择课程', '仅处理你选中的课程'), ('任务队列', '视频 / 图文 / PPT'), ('服务器已确认', '进度以服务器记录为准')]:
            label = c.create_text(250, 126, anchor='nw', text=title, fill='#71859d', font=('Microsoft YaHei UI', 9))
            value = c.create_text(250, 149, anchor='nw', text='0', fill=INK, font=('Segoe UI', 23, 'bold'))
            hint = c.create_text(300, 169, anchor='nw', text=note, fill='#8a9cb1', font=('Microsoft YaHei UI', 8))
            self.metrics.append((label, value, hint))
        # Summary stays available as a single accessible account/queue sentence.
        self.summary = ttk.Label(self.backdrop, text='账号：等待登录')

    def _build_workspace(self):
        header = ttk.Frame(self.workspace)
        header.pack(fill='x', pady=(1, 12))
        self.page_title = ttk.Label(header, text='学习任务', style='Title.TLabel')
        self.page_title.pack(side='left', anchor='center')
        self.primary_button = GlassButton(header, text='开始任务', command=self.primary_action, accent=True,
                                          width=124, height=42)
        self.primary_button.pack(side='right')
        self.more_button = GlassButton(header, text='•••', command=self.show_more, width=44, height=42)
        self.more_button.pack(side='right', padx=(8, 10))
        self.choose_button = GlassButton(header, text='选择课程', command=lambda: self.show_page('courses'), height=42)
        self.choose_button.pack(side='right')
        self.status = ttk.Label(self.workspace, text='准备就绪', font=('Microsoft YaHei UI', 10))
        self.status.pack(fill='x')
        self.scan_label = ttk.Label(self.workspace, text='选好课程后，点击开始即可自动扫描。', style='Sub.TLabel',
                                    font=('Microsoft YaHei UI', 9))
        self.scan_label.pack(fill='x', pady=(6, 9))
        progress_track = ttk.Frame(self.workspace, height=8)
        progress_track.pack(fill='x', pady=(0, 16))
        progress_track.pack_propagate(False)
        self.scan_bar = ttk.Progressbar(progress_track, mode='determinate', maximum=100,
                                        style='Glass.Horizontal.TProgressbar')
        self.scan_bar.place(x=0, y=0, relwidth=1, relheight=1)
        self.tabs = ttk.Notebook(self.workspace, style='Glass.TNotebook', takefocus=False)
        self.tabs.pack(fill='both', expand=True)
        self.course_tab, self.task_tab = ttk.Frame(self.tabs), ttk.Frame(self.tabs)
        self.warning_tab, self.logs_tab = ttk.Frame(self.tabs), ttk.Frame(self.tabs)
        for tab in (self.course_tab, self.task_tab, self.warning_tab, self.logs_tab):
            self.tabs.add(tab)
        self.tabs.bind('<<NotebookTabChanged>>', self._sync_navigation)
        self.build_course_panel()
        self._build_task_panel()
        self._build_records()
        self.more_menu = tk.Menu(self.root, tearoff=False, font=('Microsoft YaHei UI', 10),
                                 bg='white', fg=INK, activebackground='#e7f0ff', activeforeground='#1765c8', bd=0)
        for label, callback in [('重新扫描所选课程', lambda: self.command('scan')),
                                ('刷新课程与开课时间', lambda: self.command('catalog')),
                                ('重试选中任务', self.retry_selected)]:
            self.more_menu.add_command(label=label, command=callback)
        self.more_menu.add_separator()
        self.more_menu.add_command(label='查看扫描异常', command=lambda: self.show_page('warnings'))
        self.more_menu.add_command(label='打开日志文件夹', command=self.open_logs)
        self.more_menu.add_separator()
        self.more_menu.add_command(label='切换雨课堂网站…', command=self.open_site_settings)

    def build_course_panel(self):
        actions = ttk.Frame(self.course_tab)
        actions.pack(fill='x', pady=(0, 12))
        ttk.Label(actions, text='搜索', style='Sub.TLabel').pack(side='left', padx=(0, 9))
        self.course_search = tk.StringVar()
        self.course_search_entry = ttk.Entry(actions, textvariable=self.course_search, width=22)
        self.course_search_entry.pack(side='left', fill='x', expand=True, padx=(0, 12))
        self.course_search_entry.insert(0, '')
        self.course_search.trace_add('write', lambda *_: self.render_courses())
        for label, callback in [('选进行中', lambda: self.select_group('current')),
                                ('全选', lambda: self.select_group('all')),
                                ('清空', lambda: self.select_group('none'))]:
            button = GlassButton(actions, text=label, command=callback, height=36)
            button.pack(side='left', padx=(0, 6))
            self.course_buttons.append(button)
        self.course_hint = ttk.Label(self.course_tab, text='正在读取课程列表…', style='Sub.TLabel',
                                     font=('Microsoft YaHei UI', 9))
        self.course_hint.pack(fill='x', pady=(0, 10))
        self.course_table = ttk.Frame(self.course_tab)
        self.course_table.pack(fill='both', expand=True)
        columns = ('selected', 'name', 'class_name', 'start', 'end', 'state', 'note')
        self.course_tree = ttk.Treeview(self.course_table, columns=columns, show='headings', selectmode='browse')
        for key, label, width in zip(columns, ['选择', '课程', '教学班 / 学期', '开课时间', '结课时间', '状态', '说明'],
                                     [55, 170, 150, 135, 135, 90, 160]):
            self.course_tree.heading(key, text=label)
            self.course_tree.column(key, width=width, minwidth=50, stretch=key in ('name', 'class_name', 'note'))
        self.course_tree.tag_configure('ended', foreground='#95a0af')
        self.course_tree.tag_configure('chosen', foreground='#2265b4')
        self._scroll_table(self.course_table, self.course_tree)
        self.course_tree.bind('<Button-1>', self.course_click)
        self.course_tree.bind('<space>', self.course_space)
        self.course_empty = ttk.Label(self.course_table, text='登录后，课程会自动出现在这里。',
                                      style='Sub.TLabel', justify='center', anchor='center')
        ttk.Label(self.course_tab, text='点击课程行即可勾选 · 自动保存 · 时间为北京时间',
                  style='Sub.TLabel', font=('Microsoft YaHei UI', 9)).pack(anchor='w', pady=(9, 0))

    def _build_task_panel(self):
        actions = ttk.Frame(self.task_tab)
        actions.pack(fill='x', pady=(0, 12))
        ttk.Label(actions, text='搜索', style='Sub.TLabel').pack(side='left', padx=(0, 9))
        self.task_search, self.task_filter = tk.StringVar(), tk.StringVar(value='全部类型')
        self.task_search_entry = ttk.Entry(actions, textvariable=self.task_search, width=22)
        self.task_search_entry.pack(side='left', fill='x', expand=True, padx=(0, 12))
        self.task_filter_combo = ttk.Combobox(actions, textvariable=self.task_filter, state='readonly', width=12,
                                              values=['全部类型', '视频', '图文', 'PPT', '需人工处理', '失败 / 待适配'])
        self.task_filter_combo.pack(side='left', padx=(0, 12))
        self.only_unfinished = tk.BooleanVar(value=True)
        ttk.Checkbutton(actions, text='仅未完成', variable=self.only_unfinished).pack(side='right')
        self.task_search.trace_add('write', lambda *_: self.render_tasks())
        self.task_filter.trace_add('write', lambda *_: self.render_tasks())
        self.only_unfinished.trace_add('write', lambda *_: self.render_tasks())
        self.task_hint = ttk.Label(self.task_tab, text='搜索课程或任务名称', style='Sub.TLabel', font=('Microsoft YaHei UI', 9))
        self.task_hint.pack(anchor='w', pady=(0, 10))
        self.task_table = ttk.Frame(self.task_tab)
        self.task_table.pack(fill='both', expand=True)
        columns = ('course', 'title', 'kind', 'state', 'progress', 'duration', 'note')
        self.tree = ttk.Treeview(self.task_table, columns=columns, show='headings', selectmode='browse')
        for key, label, width in zip(columns, ['课程', '任务', '类型', '状态', '服务器进度', '预计时长', '说明'],
                                     [112, 174, 114, 118, 102, 82, 140]):
            self.tree.heading(key, text=label)
            self.tree.column(key, width=width, minwidth={'title': 140, 'note': 120}.get(key, 60),
                             stretch=key in ('title', 'note'))
        self._scroll_table(self.task_table, self.tree)
        self.tree.tag_configure('completed', foreground='#258566')
        self.tree.tag_configure('failed', foreground='#c36065')
        self.tree.tag_configure('needs_adapter', foreground='#aa7e41')
        self.tree.tag_configure('running', foreground='#1374dd')
        self.task_empty = ttk.Label(self.task_table, text='', style='Sub.TLabel', justify='center', anchor='center')
        ttk.Label(self.task_tab, text='任务只有在服务器确认后才会显示完成。', style='Sub.TLabel',
                  font=('Microsoft YaHei UI', 9)).pack(anchor='w', pady=(9, 0))

    def _build_records(self):
        for tab, label, other, callback in [(self.logs_tab, '最近运行记录', '查看扫描异常', lambda: self.show_page('warnings')),
                                             (self.warning_tab, '扫描异常', '返回运行记录', lambda: self.show_page('records'))]:
            bar = ttk.Frame(tab)
            bar.pack(fill='x', pady=(0, 12))
            ttk.Label(bar, text=label, style='Sub.TLabel').pack(side='left')
            GlassButton(bar, text=other, command=callback, height=34).pack(side='right')
        self.warnings = self.text_panel(self.warning_tab)
        self.logs = self.text_panel(self.logs_tab)

    @staticmethod
    def _scroll_table(parent, tree):
        vertical = ttk.Scrollbar(parent, orient='vertical', command=tree.yview)
        horizontal = ttk.Scrollbar(parent, orient='horizontal', command=tree.xview)
        tree.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        parent.rowconfigure(0, weight=1)
        parent.columnconfigure(0, weight=1)
        tree.grid(row=0, column=0, sticky='nsew')
        vertical.grid(row=0, column=1, sticky='ns')
        horizontal.grid(row=1, column=0, sticky='ew')

    def _build_site_settings(self, history):
        # The settings panel stays mounted to preserve its public widget API.
        # It is shown as an overlay, so switching sites never spawns orphan forms.
        self.site_panel = tk.Frame(self.backdrop, bg=PANEL, highlightthickness=1, highlightbackground='#d6e2f1',
                                   padx=22, pady=20)
        ttk.Label(self.site_panel, text='雨课堂网站', style='Title.TLabel').pack(anchor='w', pady=(0, 8))
        ttk.Label(self.site_panel, text='粘贴学校雨课堂首页地址，登录与任务按网站分别保存。',
                  style='Sub.TLabel', wraplength=450).pack(anchor='w', pady=(0, 14))
        self.site_address = tk.StringVar(value=self.active_site)
        self.site_entry = ttk.Combobox(self.site_panel, textvariable=self.site_address, values=history, width=46)
        self.site_entry.pack(fill='x')
        self.site_entry.bind('<Return>', lambda event: self.switch_site())
        self.site_hint = ttk.Label(self.site_panel, text=f'当前网站：{self.active_site}', style='Sub.TLabel', wraplength=460)
        self.site_hint.pack(fill='x', pady=(12, 16))
        row = ttk.Frame(self.site_panel)
        row.pack(fill='x')
        self.site_button = GlassButton(row, text='切换网站', command=self.switch_site, accent=True)
        self.site_button.pack(side='right')
        GlassButton(row, text='关闭', command=self.close_site_settings).pack(side='right', padx=8)

    def _layout(self, event=None):
        w, h = self.backdrop.winfo_width(), self.backdrop.winfo_height()
        width = max(1, w-288)
        self.backdrop.itemconfigure(self._workspace_window, width=width, height=max(1, h-280))
        self.backdrop.coords(self._site_pill_window, w-192, 42)
        self.status.configure(wraplength=width-8)
        self.scan_label.configure(wraplength=width-8)
        self.course_hint.configure(wraplength=width-12)
        available, gap = w-252, 14
        cw = (available-2*gap)/3
        for n, (label, value, hint) in enumerate(self.metrics):
            x = 250+n*(cw+gap)
            self.backdrop.coords(label, x, 127)
            self.backdrop.coords(value, x, 148)
            self.backdrop.coords(hint, x+(95 if w >= 1100 else 80), 169)
            self.backdrop.itemconfigure(hint, text=['仅处理所选课程', '自动识别类型', '以服务器为准'][n] if w < 1100 else
                                        ['仅处理你选中的课程', '视频 / 图文 / PPT', '进度以服务器记录为准'][n])
        self.backdrop.coords(self.sidebar_line, 40, h-199, 188, h-199)
        self.backdrop.coords(self.account_mark, 44, h-179, 73, h-150)
        self.backdrop.coords(self.account_initial, 58, h-165)
        self.backdrop.coords(self.account_label, 83, h-181)
        self.backdrop.coords(self.account_detail, 44, h-121)
        self.backdrop.coords(self.brand_note, 44, h-78)
        if self.site_panel.winfo_ismapped():
            self.site_panel.place(relx=.5, rely=.43, anchor='center', width=min(550, w-50))

    def _sync_navigation(self, event=None):
        lookup = {str(self.course_tab): 'courses', str(self.task_tab): 'tasks',
                  str(self.logs_tab): 'records', str(self.warning_tab): 'warnings'}
        key = lookup.get(self.tabs.select(), self._page)
        self._page = key
        for name, button in self.nav_buttons.items():
            button.configure(selected=name == ('records' if key == 'warnings' else key))
        self.page_title.configure(text={'courses': '我的课程', 'tasks': '学习任务', 'records': '运行记录', 'warnings': '扫描异常'}[key])
        self.choose_button.configure(text='查看任务' if key == 'courses' else '选择课程',
                                      command=lambda: self.show_page('tasks' if self._page == 'courses' else 'courses'))

    def show_page(self, page):
        self._page = page
        self.tabs.select({'courses': self.course_tab, 'tasks': self.task_tab,
                          'records': self.logs_tab, 'warnings': self.warning_tab}[page])
        self._sync_navigation()

    def focus_search(self, event=None):
        if self._page not in ('courses', 'tasks'):
            self.show_page('tasks')
        widget = self.course_search_entry if self._page == 'courses' else self.task_search_entry
        widget.focus_set()
        widget.selection_range(0, 'end')
        return 'break'

    def open_site_settings(self):
        self.site_panel.place(relx=.5, rely=.43, anchor='center', width=min(550, self.root.winfo_width()-50))
        self.site_panel.lift()
        self.site_entry.focus_set()

    def close_site_settings(self):
        self.site_panel.place_forget()

    def show_more(self):
        busy = self.controls_busy()
        for index in (0, 1, 2):
            self.more_menu.entryconfigure(index, state='disabled' if busy else 'normal')
        try:
            self.more_menu.tk_popup(self.more_button.winfo_rootx(), self.more_button.winfo_rooty()+45)
        finally:
            self.more_menu.grab_release()

    def controls_busy(self):
        return self.switching or self.store.get('worker_busy', False) or self.store.get('desired') in ('catalog', 'scan', 'run')

    def primary_action(self):
        if self.demo:
            self._demo_running = not self._demo_running
            self.primary_button.configure(text='暂停' if self._demo_running else '开始任务')
            self.notice('演示模式：按钮交互可体验，示例任务不会提交到真实账号。')
            return
        desired = self.store.get('desired', 'idle')
        if desired == 'login' or desired in ('catalog', 'scan', 'run'):
            self.command('paused')
        elif self.store.get('worker_busy', False):
            self.notice('正在暂停，请稍候…')
        elif not self.store.get('account', {}).get('id') or not self.store.catalog():
            # Cancelling a login wait can leave no selectable courses yet.
            # The same primary button must reconnect instead of dead-ending at
            # an empty selection page.
            self.command('catalog')
        elif not self.store.selected_courses():
            self.show_page('courses')
            self.notice('先勾选要处理的课程，再点击“开始任务”。选择会自动保存。')
        else:
            self.command('run')

    def notice(self, text, seconds=5):
        self._notice_until = time.monotonic()+seconds
        self.status.configure(text=text)

    def switch_site(self):
        if self.closing or self.switching:
            return
        if self.demo or self.base_config is None:
            self.notice('演示模式不切换真实网站')
            return
        try:
            url = normalize_site(self.site_address.get())
            config = site_config(self.base_config, url)
        except ValueError as exc:
            self.site_hint.configure(text=str(exc))
            return
        if url == self.active_site and self.watchdog and self.watchdog.is_alive():
            self.site_hint.configure(text='当前已是此网站。课程目录会在登录后自动读取；需要时可在更多菜单中刷新。')
            return
        self.switching = True
        self.site_entry.configure(state='disabled')
        self.site_button.configure(state='disabled')
        self.site_hint.configure(text='正在保存当前任务并关闭原站 Edge，再打开新网站…')
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
            self.site_hint.configure(text=f'当前网站：{url} · 登录与任务分别保存')
            self.course_search.set('')
            self.task_search.set('')
            self.show_page('courses')
            self.close_site_settings()
        except Exception as exc:
            self.store, self.active_site = old_store, old_url
            self.store.set('desired', 'paused')
            self.store.set('worker_busy', False)
            self.site_hint.configure(text=f'切换失败：{type(exc).__name__}。请检查目录权限或日志后重试。')
            try:
                save_site(self.base_config, old_url)
            except OSError:
                pass
        finally:
            self.switching = False
            self.site_entry.configure(state='normal')
            self.site_button.configure(state='normal')

    def course_click(self, event):
        if self.course_tree.identify_region(event.x, event.y) in ('cell', 'tree'):
            cid = self.course_tree.identify_row(event.y)
            self.course_tree.selection_set(cid)
            self.course_tree.focus_set()
            self.course_tree.focus(cid)
            self.toggle_course(cid)
            return 'break'

    def course_space(self, event=None):
        chosen = self.course_tree.selection()
        if chosen:
            self.toggle_course(chosen[0])
        return 'break'

    def save_selection(self, selected):
        if self.controls_busy():
            self.notice('请先暂停，等待当前操作停止后再修改课程。')
            return
        self.store.select_courses(selected)
        self.render_courses()
        self.render_tasks()
        self.notice('选择已保存。点击“开始任务”即可自动扫描并执行。')
        self.render_primary()

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
        if not hasattr(self, 'course_tree'):
            return
        courses = sorted(self.store.catalog(), key=lambda c: (c.get('start') or 0, c['classroom_id']), reverse=True)
        selected = set(self.store.selected_courses())
        query = self.course_search.get().strip().casefold()
        visible = [c for c in courses if not query or query in ' '.join(str(c.get(k, '')) for k in
                   ('name', 'class_name', 'term')).casefold()]
        ids = {c['classroom_id'] for c in visible}
        for cid in self.course_tree.get_children():
            if cid not in ids:
                self.course_tree.delete(cid)
        for index, course in enumerate(visible):
            cid = course['classroom_id']
            state = course_state(course)
            values = ('☑' if cid in selected else '☐', course['name'], course.get('class_name') or course.get('term', ''),
                      date_text(course.get('start')), date_text(course.get('end')), state, course.get('date_error', ''))
            tags = ('chosen',) if cid in selected else ('ended',) if state == '已结束' else ()
            if self.course_tree.exists(cid):
                self.course_tree.item(cid, values=values, tags=tags)
                self.course_tree.move(cid, '', index)
            else:
                self.course_tree.insert('', index, iid=cid, values=values, tags=tags)
        self.course_hint.configure(text=f'已选 {len(selected)} 门 · 共 {len(courses)} 门'
                                   + (f' · 搜索结果 {len(visible)} 门' if query else ' · 搜索课程或学期；点击整行勾选'))
        for button in self.course_buttons:
            button.configure(state='disabled' if self.controls_busy() else 'normal')
        if visible:
            self.course_empty.place_forget()
        else:
            self.course_empty.configure(text='没有匹配的课程，试试其他关键词。' if courses else
                                        '课程正在路上\n\n在 Edge 中登录后，这里会自动显示课程。\n也可在“更多”中刷新课程目录。')
            self.course_empty.place(relx=.5, rely=.48, anchor='center')

    def render_tasks(self):
        if not hasattr(self, 'tree'):
            return
        tasks = self.store.scoped_rows()
        query, kind = self.task_search.get().strip().casefold(), self.task_filter.get()
        visible = []
        for task in tasks:
            if self.only_unfinished.get() and task['state'] == 'completed':
                continue
            if query and query not in (task['course']+' '+task['title']+' '+type_label(task)).casefold():
                continue
            if kind in ('视频', '图文', 'PPT') and task['kind'] != {'视频': 'video', '图文': 'article', 'PPT': 'slides'}[kind]:
                continue
            if kind == '需人工处理' and task['state'] != 'manual':
                continue
            if kind == '失败 / 待适配' and task['state'] not in ('failed', 'needs_adapter'):
                continue
            visible.append(task)
        keys = {t['key'] for t in visible}
        for iid in self.tree.get_children():
            if iid not in keys:
                self.tree.delete(iid)
        for task in visible:
            progress = '未知' if task['progress'] is None else f"{task['progress']:.0%}"
            duration = '—' if task['duration'] is None else f"{task['duration']/60:.1f} 分钟"
            values = (task['course'], task['title'], type_label(task), STATES.get(task['state'], task['state']),
                      progress, duration, task['note'])
            if self.tree.exists(task['key']):
                self.tree.item(task['key'], values=values, tags=(task['state'],))
            else:
                self.tree.insert('', 'end', iid=task['key'], values=values, tags=(task['state'],))
        self.task_hint.configure(text=f'显示 {len(visible)} / {len(tasks)} 项 · 搜索课程或任务名称')
        if visible:
            self.task_empty.place_forget()
        else:
            if not self.store.selected_courses():
                text = '先选好课程\n\n点击“选择课程”，勾选需要处理的课程。'
            elif not self.store.scope_scanned():
                text = '准备好就开始\n\n点击“开始任务”，会先扫描所选课程，再执行任务。'
            elif tasks:
                text = '当前筛选下没有任务\n\n可以更换关键词，或取消“仅未完成”。'
            else:
                text = '暂未找到任务\n\n请查看运行记录与扫描异常，确认课程已成功扫描。'
            self.task_empty.configure(text=text)
            self.task_empty.place(relx=.5, rely=.48, anchor='center')

    def text_panel(self, parent):
        widget = tk.Text(parent, wrap='word', font=('Microsoft YaHei UI', 10), bg=PANEL, fg='#5a6e88',
                         relief='flat', padx=8, pady=8, spacing1=4, spacing3=7,
                         selectbackground='#dceaff', insertbackground=BLUE)
        scrollbar = ttk.Scrollbar(parent, command=widget.yview)
        scrollbar.pack(side='right', fill='y')
        widget.configure(yscrollcommand=scrollbar.set)
        widget.pack(fill='both', expand=True)
        return widget

    def command(self, desired):
        if self.switching:
            return
        if self.demo:
            self.notice('演示模式仅展示交互与示例数据，不会打开 Edge 或提交学习记录。')
            return
        if self.watchdog and not self.watchdog.is_alive():
            messagebox.showerror('守护进程已停止', '请查看日志并重新打开助手。')
            return
        current = self.store.get('scan_progress', {})
        if desired != 'paused' and self.controls_busy():
            self.notice('当前操作尚未停止，请先暂停后稍候。')
            return
        if desired in ('scan', 'run') and not self.store.selected_courses():
            self.show_page('courses')
            self.notice('还未选择课程。请勾选要处理的课程，然后点击开始。')
            return
        if desired in ('scan', 'catalog'):
            self.store.set('scan_progress', dict(status='queued', mode='catalog' if desired == 'catalog' else 'tasks',
                                               phase='等待读取课程时间' if desired == 'catalog' else '等待扫描任务', total=0,
                                               completed=0, succeeded=0, failed=0, tasks=0,
                                               started=time.time(), current=[]))
            self.notice('正在准备读取课程…' if desired == 'catalog' else '正在准备扫描所选课程…', 2)
        elif desired == 'run':
            # The worker's run action validates the current account and scans a
            # changed scope. Do not require a separate Scan click in the view.
            self.show_page('tasks')
            self.store.set('run_mode', 'resume' if self.store.get('desired') == 'paused' else 'start')
            self.notice('开始处理所选课程；需要时会自动扫描任务。', 2)
        elif desired == 'paused':
            if current.get('status') == 'queued':
                self.store.set('scan_progress', {**current, 'status': 'cancelled', 'message': '扫描已暂停'})
            self.notice('正在暂停，当前状态会自动保存。', 2)
        self.store.set('desired', desired)
        self.render_scan_progress()
        self.render_primary()

    def render_primary(self):
        if self.demo:
            return
        desired, busy = self.store.get('desired', 'idle'), self.store.get('worker_busy', False)
        if self.switching:
            text, disabled = '切换中…', True
        elif desired == 'login':
            text, disabled = '暂停等待', False
        elif desired in ('run', 'scan', 'catalog'):
            text, disabled = '暂停', False
        elif busy:
            text, disabled = '正在暂停…', True
        elif not self.store.get('account', {}).get('id') or not self.store.catalog():
            text, disabled = '载入课程', False
        elif desired == 'paused' and self.store.selected_courses():
            text, disabled = '继续', False
        else:
            text, disabled = '开始任务', False
        self.primary_button.configure(text=text, state='disabled' if disabled else 'normal')

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
            elapsed = max(0, int(time.time()-progress.get('started', time.time())))
            percent = f'{completed/total:.0%}' if total else '准备中'
            self.scan_label.configure(text=f"{progress.get('phase', '扫描中')}  {completed}/{total or '?'} 门（{percent}）"
                                           f" · 发现 {progress.get('tasks', 0)} 项 · 失败 {progress.get('failed', 0)} 门 · {elapsed} 秒")
        elif self.store.get('desired') == 'run':
            tasks = self.store.scoped_rows()
            done = sum(t['state'] == 'completed' for t in tasks)
            self.scan_bar.configure(maximum=max(1, len(tasks)), value=done)
            self.scan_label.configure(text=f'服务器已确认 {done} / {len(tasks)} 项 · 其余任务按状态依次处理')
        else:
            self.scan_label.configure(text=progress.get('message', '选好课程后，点击开始即可自动扫描。'))
        for label in ('开始任务', '继续', '重新扫描', '重试选中任务'):
            if label in self.buttons:
                self.buttons[label].configure(state='disabled' if scanning or self.controls_busy() else 'normal')
        self.render_primary()

    def retry_selected(self):
        if self.switching:
            return
        if self.demo:
            return self.command('run')
        selected = self.tree.selection()
        if not selected:
            self.show_page('tasks')
            self.notice('请先选中一条失败的任务，再从“更多”中重试。')
            return
        if self.controls_busy():
            self.notice('请先暂停，等待当前操作停止后再重试。')
            return
        task = next((t for t in self.store.scoped_rows() if t['key'] == selected[0]), None)
        if task and task['state'] == 'failed':
            self.store.update(task['key'], state='pending', attempts=0, note='用户重置重试预算')
            self.notice('已加入待处理队列，点击开始或继续即可重试。')
        elif task and task['state'] == 'needs_adapter':
            self.notice('此任务需要适配。请更新配置后，从“更多”中重新扫描。')

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
        if self._refresh_job:
            self.root.after_cancel(self._refresh_job)
            self._refresh_job = None
        try:
            account = self.store.get('account', {})
            tasks = self.store.scoped_rows()
            selected = self.store.selected_courses()
            done = sum(t['state'] == 'completed' for t in tasks)
            self.summary.configure(text=f"账号：{account.get('name', '等待登录')}    课程：{self.store.get('course_count', 0)}"
                                        f'（已选 {len(selected)}）    任务：{len(tasks)}    已确认：{done}    待处理：{len(tasks)-done}')
            for (_, value, _), number in zip(self.metrics, (len(selected), len(tasks), done)):
                self.backdrop.itemconfigure(value, text=str(number))
            name = account.get('name') or '等待登录'
            self.backdrop.itemconfigure(self.account_label, text='演示账号' if self.demo else name[:12])
            self.backdrop.itemconfigure(self.account_initial, text=name[0])
            self.backdrop.itemconfigure(self.account_detail, text='虚构数据 · 可体验交互' if self.demo else '登录状态自动保存')
            host = urlsplit(self.active_site).hostname or '雨课堂'
            label = '长江雨课堂' if host == 'changjiang.yuketang.cn' else '雨课堂' if host == 'www.yuketang.cn' else host
            self.site_pill.configure(text=label[:17]+'  ⌄')
            current = self.store.get('current', {})
            workflow = self.store.get('workflow', {})
            status = workflow.get('message') or self.store.get('worker_status', '准备就绪')
            scan_state = self.store.get('scan_progress', {})
            desired = self.store.get('desired', 'idle')
            if not workflow.get('message') and desired in ('idle', 'paused') and scan_state.get('status') in ('completed', 'partial', 'failed', 'cancelled'):
                status = {'completed': '扫描完成，点击开始即可处理任务', 'partial': '部分课程扫描失败，请在“更多”中查看异常',
                          'failed': '扫描失败，请查看异常后重试', 'cancelled': '已暂停，进度自动保存'}[scan_state['status']]
                if scan_state.get('mode') == 'catalog':
                    status = scan_state.get('message', status)
            if desired in ('idle', 'paused') and not selected:
                status = '请选择需要处理的课程，选好后直接开始。'
            elif desired in ('idle', 'paused') and not self.store.scope_scanned() and scan_state.get('status') not in ('failed', 'partial') and workflow.get('phase') not in ('error', 'account_changed'):
                status = '课程选择已保存，点击开始会自动扫描并执行。'
            if current and desired == 'run':
                status = f"{current.get('course', '')} / {current.get('title', '')} · {current.get('phase', status)}"
            if self.switching:
                self.status.configure(text='正在切换网站，等待原站浏览器安全退出…')
            elif self.demo and time.monotonic() >= self._notice_until:
                self.status.configure(text='演示模式 · 以下为虚构任务，可体验导航、选课和筛选。')
            elif time.monotonic() >= self._notice_until:
                self.status.configure(text=status)
            self.render_scan_progress()
            self.render_courses()
            self.render_tasks()
            errors = self.store.get('scan_errors', [])
            self.set_text(self.warnings, '\n\n'.join(f"{e['course']}\n{e['reason']}" for e in errors) or
                          '暂无已记录的扫描异常。\n\n任务列表为空不代表已经完成，请先成功扫描所选课程。')
            self.set_text(self.logs, '\n'.join(f"{time.strftime('%m-%d %H:%M:%S', time.localtime(e['time']))}  "
                                              f"{e['course']}  {e['task']}  {e['status']}  {e['reason']}" for e in self.store.events())
                          or '还没有运行记录。开始任务后，进度与异常会记录在这里。')
        finally:
            if not self.closing:
                self._refresh_job = self.root.after(650, self.refresh)

    def close(self):
        self.closing = True
        if self._refresh_job:
            self.root.after_cancel(self._refresh_job)
            self._refresh_job = None
        if self.watchdog:
            self.watchdog.stop()
            self.status.configure(text='正在保存状态并关闭受管的 Edge…')
            self.await_close()
        else:
            self.root.destroy()

    def await_close(self):
        if self.watchdog.is_alive():
            self.root.after(200, self.await_close)
        else:
            self.root.destroy()
