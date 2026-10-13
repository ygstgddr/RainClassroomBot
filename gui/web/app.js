/* The view is deliberately a small local application: all business actions go
   through two explicit pywebview methods. No DOM text is interpreted as HTML,
   and a visual progress animation never changes a task's completion state. */
(() => {
  'use strict';
  const $ = (id) => document.getElementById(id);
  const pageNames = { courses: '我的课程', tasks: '任务队列', activity: '运行记录' };
  const stateNames = { pending: '等待处理', running: '进行中', completed: '服务器已确认', manual: '人工处理', locked: '尚未开放', needs_adapter: '待适配', failed: '需要重试' };
  const typeNames = { video: '视频观看', article: '图文阅读', slides: 'PPT / 课件', homework: '作业提交', exam: '测试 / 考试', quiz: '测验', manual: '其他任务' };
  const ui = { page: 'courses', courseFilter: 'all', courseSearch: '', taskSearch: '', taskType: 'all', unfinishedOnly: true, activityTab: 'logs', pending: false, connected: false, snapshot: null, modalReturnFocus: null, navigationToken: 0 };
  const courseNodes = new Map();
  const taskNodes = new Map();
  const fingerprints = { courses: '', tasks: '', activity: '', history: '' };
  let pollTimer = null, pollRunning = false, snapshotPromise = null, pollGeneration = 0, toastTimer = null, errorsInRow = 0, filtersInitialized = false;
  const reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)');
  const asText = (value) => value === null || value === undefined ? '' : String(value);
  const number = (value, fallback = 0) => Number.isFinite(Number(value)) ? Number(value) : fallback;
  const clamp = (value, min = 0, max = 100) => Math.min(max, Math.max(min, number(value)));
  const fingerprint = (value) => JSON.stringify(value);
  const setText = (id, value) => { const node = $(id); if (node && node.textContent !== asText(value)) node.textContent = asText(value); };
  const make = (tag, className, text) => { const node = document.createElement(tag); if (className) node.className = className; if (text !== undefined) node.textContent = asText(text); return node; };
  const icon = (name) => { const node = document.createElementNS('http://www.w3.org/2000/svg', 'svg'); node.setAttribute('class', 'icon'); node.setAttribute('aria-hidden', 'true'); const use = document.createElementNS('http://www.w3.org/2000/svg', 'use'); use.setAttribute('href', '#i-' + name); node.appendChild(use); return node; };
  const actionButton = (label, action, symbol) => { const node = make('button', 'glass-button compact-button'); node.type = 'button'; node.dataset.action = action; if (symbol) node.appendChild(icon(symbol)); node.appendChild(make('span', '', label)); return node; };
  const hasAPI = () => Boolean(window.pywebview && window.pywebview.api && typeof window.pywebview.api.snapshot === 'function' && typeof window.pywebview.api.action === 'function');
  const locked = () => ui.pending || Boolean(ui.snapshot && (ui.snapshot.state.selection_locked ?? ui.snapshot.state.busy));

  function normalize(raw) {
    raw = raw && typeof raw === 'object' ? raw : {};
    const list = (key) => Array.isArray(raw[key]) ? raw[key] : [];
    return { ...raw, version: asText(raw.version), site: raw.site || {}, account: raw.account || {},
      state: { phase: 'idle', busy: false, message: '', primary: { label: '连接学习账号', action: 'load_courses', enabled: false }, ...(raw.state || {}) },
      counts: { courses: list('courses').length, selected: list('courses').filter((c) => c.selected).length, tasks: list('tasks').length, completed: 0, pending: 0, errors: list('scan_errors').length, ...(raw.counts || {}) },
      progress: raw.progress || {}, courses: list('courses'), tasks: list('tasks'), logs: list('logs'), scan_errors: list('scan_errors'), current: raw.current || {}, filters: raw.filters || {}, empty: raw.empty || {} };
  }

  function applySnapshot(raw) {
    const snapshot = normalize(raw);
    ui.snapshot = snapshot;
    ui.connected = true;
    errorsInRow = 0;
    if (!filtersInitialized) {
      ui.taskType = snapshot.filters.task_type || 'all';
      ui.unfinishedOnly = snapshot.filters.unfinished_only !== false;
      $('task-type').value = ui.taskType;
      $('unfinished-only').checked = ui.unfinishedOnly;
      filtersInitialized = true;
    }
    renderHeader();
    renderCourses();
    renderTasks();
    renderActivity();
    syncDisabled();
  }

  function renderHeader() {
    const s = ui.snapshot, phase = s.state.phase, primary = s.state.primary || {}, counts = s.counts;
    const titles = { running: '正在稳稳推进。', scanning: '正在整理学习任务。', loading_courses: '正在连接你的课程。', login_required: '登录后，继续就好。', paused: '暂歇一下，进度已保留。', pausing: '正在保存当前进度。', switching: '正在切换学习空间。', closing: '正在保存并退出。', error: '有一件事需要处理。' };
    setText('hero-title', titles[phase] || (counts.selected ? '已准备好，轻松开始。' : '选好课程，即可开始。'));
    const description = s.state.message || '选择需要处理的课程，工作台会自动扫描并安排任务。';
    setText('hero-description', description);
    setText('footer-status', description);
    setText('site-label', s.site.label || '雨课堂');
    $('site-button').title = asText(s.site.url);
    setText('account-name', s.account.name || '尚未登录');
    setText('account-sub', s.demo || window.__RAIN_DEMO__ ? '演示模式 · 虚构数据' : '登录状态保存在本机');
    const avatar = $('account-avatar');
    const initial = s.account.logged_in && s.account.name ? String(s.account.name).trim().slice(0, 1) : '';
    if (avatar.dataset.initial !== initial) { avatar.replaceChildren(initial ? document.createTextNode(initial) : icon('user')); avatar.dataset.initial = initial; }
    setText('version-label', s.version ? 'v' + s.version.replace(/^v/, '') : '');
    setText('selection-summary', counts.selected ? `${counts.selected} 门已选课程 · 自动安排任务` : '尚未选择课程');
    setText('nav-course-count', counts.courses);
    setText('nav-task-count', counts.tasks);
    setText('metric-selected', counts.selected);
    setText('metric-courses', ' / ' + counts.courses);
    setText('metric-tasks', counts.tasks);
    setText('metric-completed', counts.completed);
    setText('metric-pending', counts.pending);
    $('nav-error-dot').hidden = !counts.errors;
    setText('error-count', counts.errors);
    setText('primary-label', primary.label || '开始学习');
    const pause = primary.action === 'pause' || ['running', 'scanning', 'loading_courses', 'login_required'].includes(phase);
    const primarySymbol = pause ? 'pause' : primary.action === 'recover' ? 'refresh' : 'play';
    $('primary-icon').firstElementChild.setAttribute('href', '#i-' + primarySymbol);
    $('primary-button').classList.toggle('is-pause', pause);
    $('primary-button').setAttribute('aria-label', primary.label || '开始学习');
    $('primary-button').dataset.testid = 'primary-action';
    const busy = ['running', 'scanning', 'loading_courses', 'switching', 'pausing', 'closing'].includes(phase);
    const dot = $('connection-dot');
    dot.classList.toggle('is-working', busy);
    dot.classList.toggle('is-ready', !busy && s.account.logged_in);
    setText('connection-title', phase === 'error' ? '工作台需要恢复' : busy ? '任务正在推进' : s.account.logged_in ? '学习账号已连接' : '等待学习账号登录');
    setText('connection-sub', busy ? '进度自动保存，可随时暂停' : s.site.label ? '当前站点 · ' + s.site.label : '登录后将自动读取课程');
    setText('footer-hint', s.demo || window.__RAIN_DEMO__ ? '演示模式 · 数据均为虚构' : '选课 · 开始 · 轻松管理');
    const progressVisible = busy || (phase === 'paused' && counts.tasks > 0);
    $('workflow-progress').hidden = !progressVisible;
    document.querySelector('.hero').classList.toggle('has-progress', progressVisible);
    const p = s.progress;
    const currentDetails = s.current.title ? `${s.current.course || ''} · ${s.current.title}${s.current.phase ? ' · ' + s.current.phase : ''}` : '';
    setText('progress-label', currentDetails || p.label || (busy ? '正在处理…' : '已暂停'));
    const percent = clamp(p.percent);
    setText('progress-percent', p.indeterminate ? '准备中' : `${number(p.current)}/${number(p.total)} · ${Math.round(percent)}%`);
    $('progress-fill').style.width = percent + '%';
    $('workflow-progress').classList.toggle('is-indeterminate', Boolean(p.indeterminate || phase === 'switching' || phase === 'pausing' || phase === 'closing'));
    $('workflow-progress').setAttribute('role', 'progressbar');
    $('workflow-progress').setAttribute('aria-label', p.label || '任务进度');
    $('workflow-progress').setAttribute('aria-valuemin', '0');
    $('workflow-progress').setAttribute('aria-valuemax', '100');
    if (p.indeterminate) $('workflow-progress').removeAttribute('aria-valuenow'); else $('workflow-progress').setAttribute('aria-valuenow', String(Math.round(percent)));
  }

  function createCourseNode(course) {
    const node = make('button', 'course-card glass');
    node.type = 'button'; node.dataset.id = asText(course.id); node.dataset.testid = 'course-card'; node.setAttribute('role', 'checkbox');
    const top = make('div', 'course-top');
    const symbol = make('span', 'course-symbol'); symbol.appendChild(icon('book'));
    const check = make('span', 'course-check'); check.appendChild(icon('check'));
    top.append(symbol, check);
    const title = make('h3');
    const date = make('p', 'course-date'); date.append(icon('clock'), make('span'));
    const bottom = make('div', 'course-bottom'); bottom.append(make('span', 'pill'), make('span', 'course-selected-note'));
    node.append(top, title, date, bottom);
    node._parts = { title, date: date.lastChild, status: bottom.firstChild, note: bottom.lastChild };
    return node;
  }

  const tones = [{ tone: '#6c92c4', tint: 'rgba(142,180,232,.14)' }, { tone: '#9b86bf', tint: 'rgba(180,155,219,.13)' }, { tone: '#ab947d', tint: 'rgba(218,183,154,.12)' }, { tone: '#689b91', tint: 'rgba(135,188,171,.12)' }, { tone: '#a385a8', tint: 'rgba(199,155,205,.1)' }, { tone: '#899aba', tint: 'rgba(156,177,212,.12)' }];
  function courseTone(id) { let hash = 0; for (const char of asText(id)) hash = (hash * 31 + char.charCodeAt(0)) >>> 0; return tones[hash % tones.length]; }
  const dateShort = (text) => { const value = asText(text); return !value || /未提供|未知/.test(value) ? '时间未提供' : value.slice(0, 10); };
  function renderCourses(force = false) {
    const s = ui.snapshot; if (!s || (ui.page !== 'courses' && !force)) return;
    const courses = s.courses.filter((c) => asText(c.name).toLocaleLowerCase().includes(ui.courseSearch.toLocaleLowerCase()) && (ui.courseFilter === 'all' || (ui.courseFilter === 'current' && (c.current || c.status === '进行中')) || (ui.courseFilter === 'selected' && c.selected)));
    const hash = fingerprint([courses, locked(), ui.courseFilter, ui.courseSearch, s.empty.courses, s.account.logged_in]);
    if (!force && fingerprints.courses === hash) return;
    fingerprints.courses = hash;
    setText('course-visible-count', courses.length + ' 门');
    const container = $('course-list'), validIds = new Set(s.courses.map((c) => asText(c.id)));
    for (const [key, node] of courseNodes) if (!validIds.has(key)) { node.remove(); courseNodes.delete(key); }
    const displayedIds = new Set(courses.map((c) => asText(c.id)));
    for (const child of Array.from(container.children)) if (!displayedIds.has(child.dataset.id)) child.remove();
    courses.forEach((course, index) => {
      const key = asText(course.id);
      let node = courseNodes.get(key);
      if (!node) { node = createCourseNode(course); courseNodes.set(key, node); }
      const parts = node._parts, tone = courseTone(key);
      node.style.setProperty('--course-tone', tone.tone); node.style.setProperty('--course-tint', tone.tint);
      node.classList.toggle('is-selected', Boolean(course.selected));
      node.classList.toggle('is-disabled', locked());
      node.disabled = locked();
      node.setAttribute('aria-checked', String(Boolean(course.selected)));
      node.setAttribute('aria-label', `${asText(course.name)}，${course.selected ? '已选择' : '未选择'}，${asText(course.status || '时间未知')}`);
      const fullDate = `${dateShort(course.start_date)} → ${dateShort(course.end_date)}`;
      node.title = `${asText(course.name)}\n开课：${asText(course.start_date || '未提供')}\n结课：${asText(course.end_date || '未提供')}${course.class_name ? '\n' + course.class_name : ''}${course.message ? '\n' + course.message : ''}`;
      parts.title.textContent = asText(course.name || '未命名课程');
      parts.date.textContent = fullDate;
      parts.status.className = 'pill' + (course.current || course.status === '进行中' ? ' current' : course.status === '未开课' ? ' future' : '');
      parts.status.textContent = asText(course.status || '时间未知');
      parts.note.textContent = course.selected ? '已加入学习计划' : '点击选择';
      // Move only rows whose position actually changed. Unchanged snapshots do
      // not replace focused checkbox nodes or restart their selection animation.
      if (container.children[index] !== node) container.insertBefore(node, container.children[index] || null);
    });
    $('course-empty').hidden = Boolean(courses.length);
    if (!courses.length) {
      const filtered = s.courses.length > 0;
      renderEmpty('course-empty', filtered ? 'search' : 'book', filtered ? '没有找到匹配课程' : s.account.logged_in ? '正在等待你的课程' : '连接你的学习账号', filtered ? '试试其他关键词，或切换课程筛选条件。' : asText(s.empty.courses) || '点击连接账号，在独立 Edge 窗口登录后，课程会自动出现。', filtered ? null : ['连接并读取课程', 'load_courses', 'arrow']);
    }
  }

  function createTaskNode(task) {
    const node = make('article', 'task-row glass'); node.dataset.id = asText(task.id); node.dataset.testid = 'task-row';
    const symbol = make('span', 'task-symbol');
    const info = make('div', 'task-info'); info.append(make('h3'), make('p'));
    const type = make('span', 'task-type');
    const status = make('div', 'task-status'); status.append(make('span', 'pill'));
    const retry = make('button', 'task-retry', '重试'); retry.type = 'button'; retry.dataset.action = 'retry_task'; retry.dataset.id = asText(task.id); status.appendChild(retry);
    const progress = make('div', 'task-progress'); progress.appendChild(make('strong'));
    const track = make('div', 'progress-track'), fill = make('div', 'progress-fill'); track.append(fill); progress.appendChild(track);
    node.append(symbol, info, type, status, progress);
    node._parts = { symbol, title: info.firstChild, description: info.lastChild, type, status: status.firstChild, retry, progress: progress.firstChild, fill };
    return node;
  }

  function matchesTaskType(task) {
    const kind = asText(task.type), label = asText(task.type_label);
    if (ui.taskType === 'all') return true;
    if (ui.taskType === 'homework') return kind === 'homework' || /作业|homework/i.test(label);
    if (['exam', 'quiz'].includes(ui.taskType)) return ['exam', 'quiz'].includes(kind) || /测验|测试|考试|习题|quiz|exam/i.test(label);
    if (ui.taskType === 'failed' || ui.taskType === 'needs_adapter') return task.status === ui.taskType;
    return kind === ui.taskType;
  }
  function renderTasks(force = false) {
    const s = ui.snapshot; if (!s || (ui.page !== 'tasks' && !force)) return;
    const query = ui.taskSearch.toLocaleLowerCase();
    const tasks = s.tasks.filter((t) => `${asText(t.title)} ${asText(t.course_name)}`.toLocaleLowerCase().includes(query) && matchesTaskType(t) && (!ui.unfinishedOnly || t.status !== 'completed'));
    const hash = fingerprint([tasks, locked(), ui.taskType, ui.taskSearch, ui.unfinishedOnly, s.empty.tasks]);
    if (!force && fingerprints.tasks === hash) return;
    fingerprints.tasks = hash;
    setText('task-visible-count', tasks.length + ' 项');
    const container = $('task-list'), validIds = new Set(s.tasks.map((t) => asText(t.id))), displayedIds = new Set(tasks.map((t) => asText(t.id)));
    for (const [key, node] of taskNodes) if (!validIds.has(key)) { node.remove(); taskNodes.delete(key); }
    for (const child of Array.from(container.children)) if (!displayedIds.has(child.dataset.id)) child.remove();
    tasks.forEach((task, index) => {
      const key = asText(task.id); let node = taskNodes.get(key);
      if (!node) { node = createTaskNode(task); taskNodes.set(key, node); }
      const p = node._parts, symbol = task.type === 'video' ? 'video' : task.type === 'slides' ? 'slides' : 'file';
      if (p.symbol.dataset.symbol !== symbol) { p.symbol.replaceChildren(icon(symbol)); p.symbol.dataset.symbol = symbol; }
      p.symbol.className = 'task-symbol ' + (task.type === 'video' ? 'blue' : task.type === 'slides' ? 'violet' : ['exam', 'homework', 'manual'].includes(task.type) ? 'amber' : 'green');
      p.title.textContent = asText(task.title || '未命名任务');
      p.description.textContent = asText(task.course_name) + (task.message ? ' · ' + asText(task.message) : '');
      p.type.textContent = asText(task.type_label || typeNames[task.type] || '其他任务');
      p.status.className = 'pill ' + asText(task.status).replace(/[^a-z_]/g, '');
      p.status.textContent = asText(task.status_label || stateNames[task.status] || task.status);
      p.retry.hidden = task.status !== 'failed'; p.retry.disabled = locked();
      p.retry.setAttribute('aria-label', '重试任务：' + asText(task.title));
      const unknown = task.progress === null || task.progress === undefined;
      const percent = unknown ? 0 : clamp(number(task.progress) * 100);
      p.progress.textContent = unknown ? '未知' : `${Math.round(percent)}%`;
      p.fill.style.width = percent + '%';
      node.classList.toggle('is-running', task.status === 'running');
      node.title = `${asText(task.title)}\n${asText(task.course_name)} · ${asText(task.type_label || typeNames[task.type] || task.type)}\n${asText(task.message || task.status_label || stateNames[task.status] || '')}`;
      if (container.children[index] !== node) container.insertBefore(node, container.children[index] || null);
    });
    $('task-empty').hidden = Boolean(tasks.length);
    if (!tasks.length) {
      const filtered = s.tasks.length > 0, selected = s.counts.selected > 0;
      renderEmpty('task-empty', filtered ? 'search' : 'list', filtered ? '当前筛选没有匹配任务' : selected ? '你的学习计划已准备好' : '先选课程，再轻松开始', filtered ? '尝试更换任务类型、关键词，或关闭“仅看未完成”。' : asText(s.empty.tasks) || '直接点击开始，工作台会扫描所选课程并安排可处理的任务。', filtered ? null : selected ? ['开始学习', 'primary', 'play'] : ['选择课程', 'navigate_courses', 'arrow']);
    }
  }

  function renderActivity(force = false) {
    const s = ui.snapshot; if (!s || (ui.page !== 'activity' && !force)) return;
    const rows = ui.activityTab === 'errors' ? s.scan_errors : s.logs;
    const hash = fingerprint([rows, ui.activityTab]);
    if (!force && fingerprints.activity === hash) return;
    fingerprints.activity = hash;
    const container = $('activity-list');
    // Logs have no interactive controls. Only changed log snapshots replace
    // these small rows; the course/task lists keep their keyed DOM identities.
    const fragment = document.createDocumentFragment();
    rows.forEach((entry) => {
      const row = make('div', 'activity-row');
      row.appendChild(make('time', 'activity-time', entry.time || '扫描异常'));
      const level = /失败|异常|error|failed/i.test(asText(entry.level)) || ui.activityTab === 'errors' ? 'error' : /警告|warning/i.test(asText(entry.level)) ? 'warning' : '';
      row.appendChild(make('span', 'activity-level ' + level));
      const content = make('div', 'activity-content');
      const title = [entry.course || entry.course_name, entry.task].filter(Boolean).join(' / ');
      if (title) content.appendChild(make('strong', '', title));
      content.appendChild(make('p', '', [entry.level, entry.message].filter(Boolean).join(' · ') || '状态已更新'));
      row.appendChild(content); fragment.appendChild(row);
    });
    container.replaceChildren(fragment); container.hidden = !rows.length;
    $('activity-empty').hidden = Boolean(rows.length);
    if (!rows.length) renderEmpty('activity-empty', ui.activityTab === 'errors' ? 'shield' : 'activity', ui.activityTab === 'errors' ? '暂时没有扫描异常' : '从这里，看到每一步进展', ui.activityTab === 'errors' ? '扫描中出现的加载或适配问题会显示在这里。' : '开始处理任务后，运行记录会自动出现在这里。');
  }

  function renderEmpty(id, symbol, title, description, action) {
    const container = $(id), hash = fingerprint([symbol, title, description, action]);
    if (container.dataset.fingerprint === hash) return;
    const emblem = make('div', 'empty-icon'); emblem.appendChild(icon(symbol));
    const children = [emblem, make('h3', '', title), make('p', '', description)];
    if (action) children.push(actionButton(...action));
    container.replaceChildren(...children); container.dataset.fingerprint = hash;
  }

  function syncDisabled() {
    const s = ui.snapshot, busy = locked();
    $('primary-button').disabled = ui.pending || !s || !s.state.primary || !s.state.primary.enabled;
    $('primary-button').setAttribute('aria-busy', String(ui.pending));
    // Local navigation, searching, and logs remain available while the worker
    // runs. Commands that mutate its scope are guarded both here and in Python.
    const blocked = new Set(['load_courses', 'scan', 'retry_all', 'retry_task', 'select_current', 'select_all', 'select_clear']);
    document.querySelectorAll('[data-action]').forEach((button) => {
      if (button.tagName !== 'BUTTON') return;
      const action = button.dataset.action;
      if (blocked.has(action)) button.disabled = busy;
      else if (['open_browser', 'open_logs'].includes(action)) button.disabled = ui.pending || !ui.connected;
      else if (action === 'primary') button.disabled = $('primary-button').disabled;
    });
    $('site-submit').disabled = busy || !ui.connected;
    $('site-url').disabled = busy;
    document.querySelectorAll('.site-history button').forEach((button) => { button.disabled = busy; });
    if (!$('settings-overlay').hidden && busy) setText('site-error', '请先暂停任务，等待当前操作停止后切换站点。');
  }

  function navigate(page) {
    if (!pageNames[page] || page === ui.page) return;
    const oldPage = $('page-' + ui.page), nextPage = $('page-' + page), token = ++ui.navigationToken;
    ui.page = page;
    // Hidden pages do not consume DOM work for every background snapshot.
    // Materialize their latest display data only when the user opens them.
    if (page === 'courses') renderCourses();
    else if (page === 'tasks') renderTasks();
    else renderActivity();
    syncDisabled();
    document.querySelectorAll('[data-page]').forEach((button) => { const active = button.dataset.page === page; button.classList.toggle('is-active', active); if (active) button.setAttribute('aria-current', 'page'); else button.removeAttribute('aria-current'); });
    setText('page-breadcrumb', pageNames[page]);
    closeMenu();
    oldPage.classList.add('is-leaving');
    window.setTimeout(() => {
      if (token !== ui.navigationToken) return;
      document.querySelectorAll('.page-panel').forEach((panel) => { panel.hidden = panel !== nextPage; panel.classList.remove('is-leaving', 'is-active'); });
      nextPage.classList.add('is-active');
    }, reduceMotion.matches ? 0 : 140);
  }

  function showToast(message, error = false) {
    if (!message) return;
    const toast = $('toast'); setText('toast-text', message);
    toast.classList.toggle('is-error', error); toast.firstElementChild.firstElementChild.setAttribute('href', '#i-' + (error ? 'alert' : 'check'));
    toast.hidden = false; clearTimeout(toastTimer); toastTimer = setTimeout(() => { toast.hidden = true; }, error ? 6500 : 3300);
  }
  function closeMenu() { $('more-menu').hidden = true; $('more-trigger').setAttribute('aria-expanded', 'false'); }
  function openSettings() {
    closeMenu(); ui.modalReturnFocus = document.activeElement;
    $('site-url').value = ui.snapshot ? ui.snapshot.site.url || '' : '';
    setText('site-error', ''); renderHistory(); $('settings-overlay').hidden = false; syncDisabled();
    setTimeout(() => { if (!$('site-url').disabled) $('site-url').focus(); else $('settings-overlay').querySelector('[data-action="close_settings"]').focus(); }, 80);
  }
  function closeSettings() { $('settings-overlay').hidden = true; if (ui.modalReturnFocus && ui.modalReturnFocus.isConnected) ui.modalReturnFocus.focus(); }
  function renderHistory() {
    const site = ui.snapshot ? ui.snapshot.site : {}, history = Array.isArray(site.history) ? site.history : [];
    const hash = fingerprint([site.url, history]); if (fingerprints.history === hash) return; fingerprints.history = hash;
    const fragment = document.createDocumentFragment();
    history.forEach((entry) => {
      const url = typeof entry === 'string' ? entry : entry.url, label = typeof entry === 'string' ? entry : entry.label || entry.url;
      const button = make('button', ''); button.type = 'button'; button.textContent = asText(label); button.dataset.siteUrl = asText(url); button.title = asText(url);
      button.classList.toggle('is-active', url === site.url); fragment.appendChild(button);
    });
    $('site-history').replaceChildren(fragment);
  }

  async function sendAction(name, payload = {}) {
    if (ui.pending) return;
    if (!ui.connected) { showToast('正在连接桌面工作台，请稍候。', true); return; }
    ui.pending = true; syncDisabled(); renderCourses(); renderTasks();
    const startTime = performance.now();
    try {
      const result = hasAPI() ? await window.pywebview.api.action(name, payload) : window.__RAIN_DEMO__ ? await demoAction(name, payload) : { ok: false, message: '桌面连接暂不可用，请重新打开程序。' };
      if (result && result.navigate) navigate(result.navigate);
      if (name === 'switch_site' && result && result.ok) closeSettings();
      if (result && result.message) showToast(result.message, result.ok === false);
      if (result && result.ok === false && name === 'switch_site') setText('site-error', result.message || '站点切换未成功');
      await refreshSnapshot(true);
    } catch (error) {
      showToast('操作暂未成功，请稍后重试；详细信息可查看运行记录。', true);
    } finally {
      // A short double-click guard outlives fast local commands, while business
      // state in the next snapshot determines whether pause is immediately usable.
      const remaining = Math.max(0, 280 - (performance.now() - startTime));
      if (remaining) await new Promise((resolve) => setTimeout(resolve, remaining));
      ui.pending = false; syncDisabled(); renderCourses(); renderTasks();
    }
  }

  async function handleAction(action, target) {
    if (action === 'settings') { openSettings(); return; }
    if (action === 'close_settings') { closeSettings(); return; }
    if (action === 'navigate_courses') { navigate('courses'); return; }
    closeMenu();
    const names = { select_current: ['select_courses', { mode: 'current' }], select_all: ['select_courses', { mode: 'all' }], select_clear: ['select_courses', { mode: 'clear' }], retry_task: ['retry_tasks', { ids: [target.dataset.id] }], retry_all: ['retry_tasks', { ids: ui.snapshot ? ui.snapshot.tasks.filter((task) => task.status === 'failed').map((task) => asText(task.id)) : [] }] };
    if (action === 'retry_all' && !names.retry_all[1].ids.length) { showToast('当前没有需要重试的失败任务。'); return; }
    const command = names[action] || [action, {}]; await sendAction(...command);
  }

  document.addEventListener('click', (event) => {
    const course = event.target.closest('.course-card');
    if (course && !course.disabled && ui.snapshot) { const entry = ui.snapshot.courses.find((c) => asText(c.id) === course.dataset.id); if (entry) sendAction('select_course', { id: course.dataset.id, selected: !entry.selected }); return; }
    const page = event.target.closest('[data-page]'); if (page) { navigate(page.dataset.page); return; }
    const action = event.target.closest('[data-action]'); if (action && !action.disabled) { handleAction(action.dataset.action, action); return; }
    const courseFilter = event.target.closest('[data-course-filter]');
    if (courseFilter) { ui.courseFilter = courseFilter.dataset.courseFilter; document.querySelectorAll('[data-course-filter]').forEach((node) => node.classList.toggle('is-active', node === courseFilter)); renderCourses(); return; }
    const activity = event.target.closest('[data-activity-tab]');
    if (activity) { ui.activityTab = activity.dataset.activityTab; document.querySelectorAll('[data-activity-tab]').forEach((node) => node.classList.toggle('is-active', node === activity)); renderActivity(); return; }
    const history = event.target.closest('[data-site-url]'); if (history && !history.disabled) { $('site-url').value = history.dataset.siteUrl; setText('site-error', ''); $('site-url').focus(); return; }
    if (event.target === $('settings-overlay')) closeSettings();
    if (!event.target.closest('.hero-actions')) closeMenu();
  });
  $('primary-button').addEventListener('click', () => sendAction('primary'));
  $('more-trigger').addEventListener('click', () => { const open = $('more-menu').hidden; $('more-menu').hidden = !open; $('more-trigger').setAttribute('aria-expanded', String(open)); });
  document.querySelector('.brand').addEventListener('click', (event) => { event.preventDefault(); navigate('courses'); });
  $('course-search').addEventListener('input', (event) => { ui.courseSearch = event.target.value; renderCourses(); });
  $('task-search').addEventListener('input', (event) => { ui.taskSearch = event.target.value; renderTasks(); });
  const taskFilterChanged = () => { ui.taskType = $('task-type').value; ui.unfinishedOnly = $('unfinished-only').checked; renderTasks(); if (ui.connected && !ui.pending) sendAction('task_filter', { type: ui.taskType, unfinished_only: ui.unfinishedOnly }); };
  $('task-type').addEventListener('change', taskFilterChanged); $('unfinished-only').addEventListener('change', taskFilterChanged);
  $('site-form').addEventListener('submit', (event) => { event.preventDefault(); if (locked()) return; const url = $('site-url').value.trim(); if (!url) { setText('site-error', '请输入学校使用的雨课堂主页地址。'); return; } sendAction('switch_site', { url }); });
  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape') { if (!$('settings-overlay').hidden) closeSettings(); else closeMenu(); }
    if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'f') { event.preventDefault(); if (ui.page === 'courses') $('course-search').focus(); else if (ui.page === 'tasks') $('task-search').focus(); }
    // Keep modal keyboard navigation inside its dialog; restore focus on close.
    if (event.key === 'Tab' && !$('settings-overlay').hidden) {
      const items = Array.from($('settings-overlay').querySelectorAll('button:not(:disabled), input:not(:disabled), [tabindex="0"]')).filter((node) => node.getClientRects().length);
      if (!items.length) return;
      if (event.shiftKey && document.activeElement === items[0]) { event.preventDefault(); items[items.length - 1].focus(); }
      else if (!event.shiftKey && document.activeElement === items[items.length - 1]) { event.preventDefault(); items[0].focus(); }
    }
  });

  async function refreshSnapshot(fresh = false) {
    // A command waits for any earlier read to finish, then requests a new read.
    // This prevents an old in-flight snapshot from undoing a freshly saved
    // course selection or briefly making a running Start button usable again.
    if (pollRunning) { if (snapshotPromise) await snapshotPromise; if (!fresh) return; }
    pollRunning = true;
    const operation = (async () => {
      try {
        if (hasAPI()) applySnapshot(await window.pywebview.api.snapshot());
        else if (window.__RAIN_DEMO__) applySnapshot(typeof window.__RAIN_DEMO__ === 'object' ? window.__RAIN_DEMO__ : demoSnapshot());
        else if (!ui.connected) { setText('hero-title', '正在连接桌面工作台。'); setText('hero-description', '连接完成后，可选择课程并开始处理任务。'); }
      } catch (error) {
        errorsInRow += 1;
        if (errorsInRow >= 3) { setText('connection-title', '桌面连接暂时中断'); setText('connection-sub', '正在重新连接，已保存的进度保留'); setText('footer-status', '正在重新连接工作台…'); $('connection-dot').classList.remove('is-ready', 'is-working'); }
      }
    })();
    snapshotPromise = operation;
    try { await operation; } finally { pollRunning = false; snapshotPromise = null; }
  }
  async function poll() { const generation = ++pollGeneration; clearTimeout(pollTimer); await refreshSnapshot(); if (generation === pollGeneration) pollTimer = setTimeout(poll, document.hidden ? 8000 : 1200); }
  window.addEventListener('pywebviewready', () => { poll(); });
  document.addEventListener('visibilitychange', () => { document.body.classList.toggle('is-background', document.hidden); if (!document.hidden) poll(); });

  /* Explicit fixture injection is the only standalone demo path. Production
     never invents an account or courses when its backend is unavailable. */
  function demoSnapshot() {
    const courses = [
      { id: 'demo-c1', name: '普通物理（二）', start_date: '2026-09-01 08:00', end_date: '2027-01-12 18:00', status: '进行中', current: true, selected: true },
      { id: 'demo-c2', name: '大学英语（三）', start_date: '2026-09-01 08:00', end_date: '2027-01-10 18:00', status: '进行中', current: true, selected: true },
      { id: 'demo-c3', name: '高等数学（下）', start_date: '2026-09-01 08:00', end_date: '2027-01-15 18:00', status: '进行中', current: true, selected: false },
      { id: 'demo-c4', name: '计算机基础与 Python 程序设计', start_date: '2026-09-03 08:00', end_date: '2027-01-18 18:00', status: '进行中', current: true, selected: false },
      { id: 'demo-c5', name: '艺术鉴赏与生活美学', start_date: '2026-09-08 08:00', end_date: '2027-01-08 18:00', status: '进行中', current: true, selected: true },
      { id: 'demo-c6', name: '微积分入门', start_date: '2026-03-01 08:00', end_date: '2026-07-01 18:00', status: '已结束', current: false, selected: false },
    ];
    const tasks = [
      { id: 'demo-t1', course_name: '普通物理（二）', title: '光的干涉 · 双缝实验', type: 'video', type_label: '视频观看', status: 'running', status_label: '播放中', progress: .42 },
      { id: 'demo-t2', course_name: '普通物理（二）', title: '第九章 · 光学基础课件', type: 'slides', type_label: 'PPT 阅读', status: 'pending', status_label: '等待处理', progress: .3 },
      { id: 'demo-t3', course_name: '大学英语（三）', title: 'Unit 3 · Reading and Reflection', type: 'article', type_label: '图文阅读', status: 'pending', status_label: '等待处理', progress: 0 },
      { id: 'demo-t4', course_name: '艺术鉴赏与生活美学', title: '如何欣赏一幅画', type: 'video', type_label: '视频观看', status: 'completed', status_label: '服务器已确认', progress: 1 },
      { id: 'demo-t5', course_name: '大学英语（三）', title: 'Unit 3 · 课堂作业', type: 'manual', type_label: '作业提交', status: 'manual', status_label: '人工处理', progress: null },
    ];
    return { demo: true, version: '0.6.0', site: { label: '长江雨课堂', url: 'https://changjiang.yuketang.cn/web', history: [{ label: '雨课堂', url: 'https://www.yuketang.cn/v2/web' }, { label: '长江雨课堂', url: 'https://changjiang.yuketang.cn/web' }] }, account: { name: '演示同学', logged_in: true }, state: { phase: 'idle', busy: false, selection_locked: false, message: '课程选择已保存。直接点击开始，自动扫描并安排学习任务。', primary: { label: '开始学习', action: 'start', enabled: true } }, counts: { courses: courses.length, selected: 3, tasks: tasks.length, completed: 1, pending: 3, errors: 0 }, courses, tasks, logs: [{ time: '10-13 15:24:12', level: '服务器已确认', course: '艺术鉴赏与生活美学', task: '如何欣赏一幅画', message: '任务完成状态已同步。' }, { time: '10-13 15:23:48', level: '扫描完成', message: '所选课程任务已加入队列。' }], scan_errors: [], progress: { current: 1, total: 5, percent: 20, label: '学习进度' }, filters: { task_type: 'all', unfinished_only: true } };
  }
  async function demoAction(name, payload) {
    const s = normalize(ui.snapshot || demoSnapshot());
    if (name === 'select_course') s.courses = s.courses.map((course) => asText(course.id) === payload.id ? { ...course, selected: payload.selected } : course);
    if (name === 'select_courses') s.courses = s.courses.map((course) => ({ ...course, selected: payload.mode === 'all' || payload.mode === 'current' && course.current }));
    s.counts.selected = s.courses.filter((course) => course.selected).length;
    if (name === 'primary') {
      if (!s.counts.selected) return { ok: true, message: '请先选择课程', navigate: 'courses' };
      const paused = s.state.phase === 'running';
      s.state = { phase: paused ? 'paused' : 'running', busy: !paused, selection_locked: !paused, message: paused ? '已暂停，点击继续即可恢复。' : '演示运行中 · 不连接真实学习账号。', primary: { label: paused ? '继续学习' : '暂停学习', action: paused ? 'start' : 'pause', enabled: true } };
    }
    if (name === 'switch_site') { s.site = { ...s.site, url: payload.url, label: payload.url.includes('changjiang.') ? '长江雨课堂' : '雨课堂' }; }
    if (name === 'task_filter') s.filters = { task_type: payload.type, unfinished_only: payload.unfinished_only };
    window.__RAIN_DEMO__ = s; applySnapshot(s); return { ok: true, message: '演示操作已更新' };
  }
  // A narrow UI-only hook assists offline visual/interaction regression checks.
  // It exposes the already-public display snapshot, never the business Store.
  window.RainUI = Object.freeze({ applySnapshot, getState: () => ({ page: ui.page, courseFilter: ui.courseFilter, taskType: ui.taskType, unfinishedOnly: ui.unfinishedOnly, pending: ui.pending, snapshot: ui.snapshot }), navigate, demoSnapshot });
  $('primary-button').dataset.testid = 'primary-action';
  poll();
})();
