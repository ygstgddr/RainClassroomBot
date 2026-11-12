/* The view is deliberately a small local application: all business actions go
   through two explicit pywebview methods. No DOM text is interpreted as HTML,
   and a visual progress animation never changes a task's completion state. */
(() => {
  'use strict';
  const $ = (id) => document.getElementById(id);
  const typingWindow = document.documentElement.classList.contains('typing-window');
  const pageNames = { courses: '我的课程', tasks: '任务队列', activity: '运行记录', browser: '浏览器', tools: '其他功能' };
  const stateNames = { pending: '等待处理', running: '进行中', completed: '服务器已确认', manual: '人工处理', locked: '尚未开放', needs_adapter: '待适配', failed: '需要重试' };
  const typeNames = { video: '视频观看', article: '图文阅读', slides: 'PPT / 课件', homework: '作业提交', exam: '测试 / 考试', quiz: '测验', manual: '其他任务' };
  const ui = { page: 'courses', courseFilter: 'all', courseSearch: '', taskSearch: '', taskType: 'all', unfinishedOnly: true, activityTab: 'logs', pending: false, windowPending: false, connected: false, snapshot: null, modalReturnFocus: null, navigationToken: 0, confirmedTarget: '', typingTarget: '', typingSpeedDirty: false };
  const courseNodes = new Map();
  const taskNodes = new Map();
  const selectedTasks = new Set();
  let taskGroup = 'automatic', taskAccountScope = '';
  const taskAutomatic = (t) => t.execution_group ? t.execution_group === 'automatic' : ['video', 'article', 'slides'].includes(t.type) && !['manual', 'needs_adapter'].includes(t.status);
  const taskSelectable = (t) => t.selectable ?? (taskAutomatic(t) && ['pending', 'failed'].includes(t.status));
  const taskHidden = (t) => t.hidden_until_open === true || (t.status === 'locked' && (t.type === 'homework' || /作业/.test(t.type_label || '')));
  const fingerprints = { courses: '', tasks: '', activity: '', history: '', assessment: '', chat: '' };
  let assessmentTab = 'chat', assessmentKeySettings = false;
  let assessmentFeedback = '';
  const assessmentEfforts = ['none', 'low', 'high', 'max'];
  const assessmentEffortLabels = ['快速', '低', '高', '最大'];
  let pollTimer = null, pollRunning = false, snapshotPromise = null, pollGeneration = 0, toastTimer = null, errorsInRow = 0, filtersInitialized = false;
  let browserRect, browserLayoutFrame = 0, browserLayoutCall = Promise.resolve();
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
  const loginGate = () => !ui.snapshot || ui.snapshot.state.login_gate === true;

  function normalize(raw) {
    raw = raw && typeof raw === 'object' ? raw : {};
    const list = (key) => Array.isArray(raw[key]) ? raw[key] : [];
    return { ...raw, version: asText(raw.version), site: raw.site || {}, account: raw.account || {},
      state: { phase: 'idle', busy: false, message: '', primary: { label: '连接学习账号', action: 'load_courses', enabled: false }, ...(raw.state || {}) },
      counts: { courses: list('courses').length, selected: list('courses').filter((c) => c.selected).length, tasks: list('tasks').length, completed: 0, pending: 0, errors: list('scan_errors').length, ...(raw.counts || {}) },
      progress: raw.progress || {}, courses: list('courses'), tasks: list('tasks'), logs: list('logs'), scan_errors: list('scan_errors'), current: raw.current || {}, filters: raw.filters || {}, empty: raw.empty || {},
      typing: { phase: 'idle', available: false, sent: 0, total: 0, speed: 20, message: '', ...(raw.typing || {}) },
      window: { maximized: false, native_controls: false, ...(raw.window || {}) } };
  }

  function applySnapshot(raw) {
    const snapshot = normalize(raw);
    ui.snapshot = snapshot;
    ui.connected = true;
    errorsInRow = 0;
    if (typingWindow) { renderTools(); renderWindowFrame(); return; }
    $('typing-launcher').hidden = !snapshot.separate_typing;
    $('typing-editor').hidden = Boolean(snapshot.separate_typing);
    if (!filtersInitialized) {
      ui.taskType = snapshot.filters.task_type || 'all';
      ui.unfinishedOnly = snapshot.filters.unfinished_only !== false;
      $('task-type').value = ui.taskType;
      $('unfinished-only').checked = ui.unfinishedOnly;
      filtersInitialized = true;
    }
    renderLogin();
    renderBrowser();
    renderHeader();
    renderCourses();
    renderTasks();
    renderActivity();
    if (!snapshot.separate_typing) renderTools();
    renderWindowFrame();
    syncDisabled();
  }

  function renderBrowser() {
    const s = ui.snapshot, b = s?.browser || {};
    document.querySelector('[data-page="browser"]').hidden = !b.enabled;
    const login = Boolean(b.enabled && loginGate() && b.login_surface);
    const host = $('login-browser-host'), panel = $('browser-panel');
    const container = login ? host : $('page-browser');
    if (panel.parentElement !== container) container.appendChild(panel);
    host.hidden = !login;
    if (login) $('login-screen').hidden = true;
    setText('browser-status', b.message || '点击登录后连接');
    const address = $('browser-address');
    if (document.activeElement !== address) address.value = b.url || s?.site.url || '';
    const canBrowse = !ui.pending && b.can_navigate && ['ready', 'loading', 'navigation_error'].includes(b.phase);
    address.disabled = $('browser-go').disabled = !canBrowse;
    document.querySelectorAll('[data-browser-command]').forEach(button => {
      const cmd = button.dataset.browserCommand;
      button.disabled = !canBrowse || (cmd === 'back' && !b.back) || (cmd === 'forward' && !b.forward) || (cmd === 'stop' && b.phase !== 'loading');
    });
    setText('browser-mute', b.muted === false ? '静音' : '解除静音');
    $('browser-pause').hidden = !b.can_pause;
    $('browser-pause').disabled = ui.pending;
    $('browser-recover').hidden = b.phase !== 'error' && s?.state.phase !== 'error' && s?.state.login_status !== 'error';
    $('browser-typing-tools').hidden = login || !(s?.separate_typing || s?.typing.target);
    renderAssessment();
    const m = b.media || {};
    const time = seconds => Number.isFinite(seconds) ? `${Math.floor(seconds / 60)}:${String(Math.floor(seconds % 60)).padStart(2, '0')}` : '—';
    setText('browser-media', m.state ? `${m.state} · ${time(m.time)} / ${time(m.duration)} · ${m.rate}×` : '未检测到视频');
    requestBrowserLayout();
  }

  function renderAssessment() {
    const s = ui.snapshot, a = s?.assessment || {};
    const button = $('browser-assessment-tools'), panel = $('assessment-panel');
    button.hidden = loginGate() || !s?.browser?.enabled || !s.assessment;
    if (button.hidden) {
      panel.hidden = true;
      setAssessmentEffortOpen(false);
      button.setAttribute('aria-expanded', 'false');
      $('assessment-key').value = '';
      return;
    }
    const scanning = ['queued', 'running'].includes(a.phase);
    const opening = ['queued', 'opening'].includes(a.entry?.phase);
    const busy = ui.pending || Boolean(a.operation);
    const browserBusy = busy || s.state.busy || !a.available;
    const keyOpen = !a.has_key || assessmentKeySettings;
    const chatting = !keyOpen && assessmentTab === 'chat';
    const tasksOpen = !keyOpen && assessmentTab === 'tasks';
    $('assessment-reconnect').hidden = Boolean(a.available);
    $('assessment-reconnect').querySelector('button').disabled = ui.pending;
    $('assessment-key-form').hidden = !keyOpen;
    $('assessment-key-settings').disabled = busy || !a.has_key;
    $('assessment-key-settings').setAttribute('aria-expanded', String(keyOpen));
    $('assessment-clear-key').hidden = !a.has_key;
    $('assessment-key').disabled = $('assessment-save-key').disabled = $('assessment-clear-key').disabled = busy;
    $('assessment-agent-controls').hidden = keyOpen;
    $('assessment-chat-view').hidden = !chatting;
    $('assessment-task-view').hidden = !tasksOpen;
    $('assessment-dock').hidden = keyOpen;
    $('assessment-chat-form').hidden = $('assessment-read-toggle').hidden = $('assessment-send').hidden = !chatting;
    document.querySelectorAll('.assessment-nav button').forEach(item => {
      const active = item.dataset.action === 'assessment_tab_' + assessmentTab;
      item.classList.toggle('is-active', active);
      item.setAttribute('aria-pressed', String(active));
    });
    $('assessment-model').disabled = $('assessment-model-toggle').disabled = $('assessment-effort').disabled = $('assessment-effort-toggle').disabled = busy;
    if (busy || keyOpen || panel.hidden) { setAssessmentEffortOpen(false); setAssessmentModelOpen(false); }
    $('assessment-web').disabled = busy || $('assessment-model').value !== 'deepseek-v4-pro';
    $('assessment-cancel').hidden = !scanning && !opening && !a.operation;
    $('assessment-cancel').disabled = ui.pending;
    $('assessment-scan').disabled = browserBusy || !a.has_key;
    $('assessment-review').disabled = browserBusy || !a.has_key || s.browser.phase !== 'ready';
    $('assessment-read-started').hidden = !chatting;
    $('assessment-read-started').disabled = $('assessment-review').disabled;
    $('assessment-read-page').disabled = browserBusy;
    if (!a.available) $('assessment-read-page').checked = false;
    $('assessment-chat-input').disabled = $('assessment-send').disabled = busy || !a.has_key;
    $('assessment-clear-chat').disabled = busy || keyOpen || !a.history?.length;
    if (panel.hidden) return;

    const stats = a.stats || {}, failed = a.failed || [];
    const tasks = (a.tasks || []).filter(task => task.state !== 'completed');
    const types = tasks.reduce((counts, task) => counts.set(task.kind, (counts.get(task.kind) || 0) + 1), new Map());
    const breakdown = [['homework', '作业'], ['quiz', '测验'], ['exam', '考试']]
      .filter(([kind]) => types.has(kind)).map(([kind, label]) => `${label} ${types.get(kind)}`);
    const progress = scanning ? `正在扫描 ${stats['已扫描课程'] || 0} / ${stats['课程数'] || '…'} 门课程` :
      tasks.length ? [`${tasks.length} 项未完成`, ...breakdown].join(' · ') : '';
    setText('assessment-progress', progress);
    $('assessment-progress').hidden = !progress;
    const summary = a.operation === 'summary' ? 'DeepSeek 正在汇总…' : a.summary || a.scan_message;
    $('assessment-summary-details').hidden = !summary && !failed.length;
    setText('assessment-summary-title', failed.length ? `扫描详情 · ${failed.length} 门课程读取失败` : '扫描详情');
    setText('assessment-summary', summary);
    $('assessment-summary').hidden = !summary;
    $('assessment-failed-courses').hidden = !failed.length;
    $('assessment-task-count').hidden = !tasks.length;
    setText('assessment-task-count', tasks.length);
    setText('assessment-message', assessmentFeedback || a.message ||
      (a.operation === 'agent' && chatting ? '' : a.entry?.message || ''));

    $('assessment-tasks').hidden = !tasks.length;
    $('assessment-task-empty').hidden = Boolean(tasks.length);
    if (!tasks.length) {
      const empty = scanning ? ['正在扫描课程', '发现未完成的任务后会显示在这里。', 'refresh'] :
        a.phase === 'done' && !failed.length ? ['没有待办任务', '当前已扫描的课程中没有未完成任务。', 'check'] :
        failed.length || a.phase === 'error' ? ['暂未找到待办任务', '可查看扫描详情，或重新扫描课程。', 'list'] :
        a.phase === 'cancelled' ? ['扫描已停止', '再次扫描以查找未完成的任务。', 'list'] :
        ['还没有扫描任务', '点击「扫描任务」查看当前账号的待办。', 'list'];
      setText('assessment-empty-title', empty[0]);
      setText('assessment-empty-note', empty[1]);
      $('assessment-empty-icon').setAttribute('href', '#i-' + empty[2]);
    }

    const hash = fingerprint([tasks, failed, browserBusy]);
    if (fingerprints.assessment !== hash) {
      fingerprints.assessment = hash;
      $('assessment-failed-courses').replaceChildren(...failed.map(name => make('li', '', name)));
      const list = document.createDocumentFragment();
      const occurrences = new Map();
      const taskName = t => JSON.stringify([t.course, t.title]);
      tasks.forEach(t => occurrences.set(taskName(t), (occurrences.get(taskName(t)) || 0) + 1));
      tasks.forEach(t => {
        const item = make('li', 'assessment-task');
        const mark = make('span', 'assessment-mark');
        mark.append(icon(t.state === 'locked' ? 'clock' : 'file'));
        const copy = make('div');
        const classroom = occurrences.get(taskName(t)) > 1 ? ` · 班级 ${asText(t.id).split(':')[0]}` : '';
        const state = ({ locked: '未开放', manual: '可核对', unknown: '进度未知' })[t.state] || t.state;
        const status = make('span', 'assessment-task-status', state);
        if (t.state === 'manual') status.classList.add('is-manual');
        const meta = make('p', 'assessment-task-meta');
        meta.append(make('span', '', `${t.course}${classroom} · ${typeNames[t.kind] || t.kind}`), status);
        copy.append(make('strong', '', t.title), meta);
        item.append(mark, copy);
        if (t.state !== 'locked') {
          const open = actionButton('交给 Agent', 'assessment_select_task');
          open.title = '自动进入此任务，可能立即开始计时';
          open.dataset.id = t.id;
          open.disabled = browserBusy;
          item.append(open);
        }
        list.append(item);
      });
      $('assessment-tasks').replaceChildren(list);
    }
    renderAssessmentChat(a.history || [], a.operation === 'agent' ? (a.steps || []).at(-1) || '正在请求模型' : '', a.fill_receipt);
  }

  function assessmentReceipt(result) {
    if (!result || (!Array.isArray(result.filled) && !result.error)) return null;
    const card = make('div', 'assessment-receipt');
    card.append(make('strong', '', result.filled ? `填写回执 · 已写入 ${result.filled.length} 题` : '填写回执 · 未能写入'));
    if (result.error) card.append(make('p', '', result.error));
    if (result.submission) {
      const state = result.submission.state;
      const label = state === 'submitted' ? '已确认提交' : state === 'blocked' ? '未提交' : '提交结果待确认';
      card.append(make('p', 'assessment-submission', `${label} · ${result.submission.message || ''}`));
    }
    const notes = make('ul');
    Object.entries(result.review_notes || {}).forEach(([id, reason]) => {
      notes.append(make('li', '', `第 ${id} 题 · 未通过复核 · ${reason}`));
    });
    Object.entries(result.errors || {}).forEach(([id, reason]) => {
      notes.append(make('li', '', `第 ${id} 题 · 未写入 · ${reason}`));
    });
    if (result.corrected?.length) {
      notes.append(make('li', '', `按两轮复核结果修正并写入 · 第 ${result.corrected.join('、')} 题`));
    }
    if (notes.childElementCount) card.append(notes);
    return card;
  }

  function formatAssessmentResponse(text) {
    const response = make('div', 'assessment-response');
    const appendInline = (node, line) => {
      line.split(/(\*\*[^*\n]+\*\*)/).forEach((part, index) => {
        node.append(index % 2 ? make('strong', '', part.slice(2, -2)) : part);
      });
    };
    let block;
    for (const line of asText(text).split(/\r?\n/)) {
      if (block?.localName === 'pre') {
        block.append('\n' + line);
        if (/^\s*```\s*$/.test(line)) block = null;
        continue;
      }
      if (/^\s*```/.test(line)) {
        block = make('pre', '', line);
        response.append(block);
        continue;
      }
      if (!line.trim()) {
        block = null;
        continue;
      }
      const heading = line.match(/^#{1,6}[ \t]+(.+)$/);
      if (heading) {
        const title = make('h4');
        appendInline(title, heading[1]);
        response.append(title);
        block = null;
        continue;
      }
      const item = line.match(/^(?:[-*+]|(\d{1,9})\.)[ \t]+(.+)$/);
      const tag = item ? (item[1] ? 'ol' : 'ul') : 'p';
      if (block?.localName !== tag) {
        block = make(tag);
        if (tag === 'ol') block.start = Number(item[1]);
        response.append(block);
      } else if (!item) {
        block.append('\n');
      }
      if (item) {
        const row = make('li');
        appendInline(row, item[2]);
        block.append(row);
      } else {
        appendInline(block, line);
      }
    }
    return response;
  }

  function renderAssessmentChat(history, liveStep, receipt) {
    const hash = fingerprint([history, liveStep, receipt]);
    if (fingerprints.chat === hash) return;
    fingerprints.chat = hash;
    const log = $('assessment-chat'), messages = document.createDocumentFragment();
    const top = log.scrollTop;
    const nearBottom = log.scrollHeight - top - log.clientHeight < 55;
    const openDetails = new Set([...log.querySelectorAll('details[open]')].map(node => node.dataset.messageKey));
    if (!history.length) {
      const welcome = make('div', 'assessment-welcome');
      const mark = make('span', 'assessment-mark');
      mark.append(icon('sparkles'));
      welcome.append(mark, make('h4', '', '想聊点什么？'),
        make('p', '', '在这里与 DeepSeek 聊天。需要填写、核对并提交时，可从待办任务启动自动执行。'),
        actionButton('查看任务待办', 'assessment_tab_tasks', 'list'));
      messages.append(welcome);
    }
    history.forEach((item, index) => {
      const user = item.role === 'user';
      const entry = make('article', user ? 'assessment-message assessment-user' : 'assessment-message');
      entry.setAttribute('aria-label', user ? '你' : 'DeepSeek');
      if (!user) {
        const label = make('div', 'assessment-assistant-label');
        const mark = make('span', 'assessment-assistant-mark');
        mark.append(icon('sparkles'));
        label.append(mark, make('span', '', 'DeepSeek'));
        entry.append(label);
      }
      if (item.reasoning || item.steps?.length) {
        const details = make('details', 'assessment-reasoning');
        details.dataset.messageKey = `${index}:${asText(item.text).slice(0, 100)}`;
        details.open = openDetails.has(details.dataset.messageKey);
        details.append(make('summary', '', item.reasoning ? '推理与工具步骤' : '工具步骤'));
        if (item.steps?.length) {
          const steps = make('ol', 'assessment-steps');
          item.steps.forEach(step => steps.append(make('li', '', step)));
          details.append(steps);
        }
        if (item.reasoning) details.append(make('p', 'assessment-chat-text', item.reasoning));
        entry.append(details);
      }
      entry.append(user ? make('p', 'assessment-chat-text', item.text) : formatAssessmentResponse(item.text));
      const sources = make('div', 'assessment-sources');
      (item.sources || []).forEach(source => {
        const link = make('a', 'assessment-source', source.title || source.url);
        try {
          const url = new URL(source.url);
          if (url.protocol !== 'https:' && url.protocol !== 'http:') return;
          link.href = url.href; link.target = '_blank'; link.rel = 'noopener noreferrer';
          sources.append(link);
        } catch { /* omit invalid source */ }
      });
      if (sources.childElementCount) entry.append(sources);
      const toolResult = assessmentReceipt(item.receipt);
      if (toolResult) entry.append(toolResult);
      if (!user && item.text) {
        const copy = make('button', 'assessment-copy', '复制回复');
        copy.type = 'button';
        copy.addEventListener('click', () => copyAssessmentReply(item.text));
        entry.append(copy);
      }
      messages.append(entry);
    });
    const toolResult = assessmentReceipt(receipt);
    if (toolResult) messages.append(toolResult);
    if (liveStep) messages.append(make('p', 'assessment-live', liveStep));
    log.replaceChildren(messages);
    log.scrollTop = nearBottom || history.at(-1)?.role === 'user' ? log.scrollHeight : top;
  }

  async function copyAssessmentReply(text) {
    try {
      await navigator.clipboard.writeText(text);
    } catch {
      const field = make('textarea', 'assessment-copy-buffer');
      field.value = text;
      field.readOnly = true;
      const focused = document.activeElement;
      document.body.append(field);
      let copied = false;
      try {
        field.select();
        copied = document.execCommand('copy');
      } catch {
        copied = false;
      } finally {
        field.remove();
        focused?.focus({ preventScroll: true });
      }
      if (!copied) {
        showToast('复制未成功，可选中回复正文手动复制。', true);
        return;
      }
    }
    showToast('已复制回复。');
  }

  function setAssessmentEffortOpen(open, restoreFocus = false) {
    const toggle = $('assessment-effort-toggle');
    const popover = $('assessment-effort-popover');
    if (popover.hidden === !open) return;
    popover.hidden = !open;
    toggle.setAttribute('aria-expanded', String(open));
    if (open) { setAssessmentModelOpen(false); $('assessment-effort').focus(); }
    if (restoreFocus) toggle.focus();
  }

  function setAssessmentModelOpen(open, restoreFocus = false) {
    const toggle = $('assessment-model-toggle');
    if (open && toggle.disabled) return;
    $('assessment-model-menu').hidden = !open;
    toggle.setAttribute('aria-expanded', String(open));
    if (open) {
      setAssessmentEffortOpen(false);
      $('assessment-model-menu').querySelector('[aria-checked=true]').focus();
    }
    if (restoreFocus) toggle.focus();
  }

  function assessmentOptions() {
    const model = $('assessment-model').value;
    return { model, effort: assessmentEfforts[$('assessment-effort').valueAsNumber], web_search: model === 'deepseek-v4-pro' && $('assessment-web').checked };
  }

  function syncBrowserLayout() {
    if (!hasAPI() || !ui.snapshot?.browser?.enabled) return Promise.resolve();
    const el = $('browser-viewport');
    const visible = (loginGate() ? !$('login-browser-host').hidden : ui.page === 'browser') && $('settings-overlay').hidden;
    const r = el.getBoundingClientRect();
    const x = Math.max(0, r.left), y = Math.max(0, r.top);
    const w = Math.min(innerWidth, r.right) - x, h = Math.min(innerHeight, r.bottom) - y;
    const rect = visible && w > 0 && h > 0 ? [x / innerWidth, y / innerHeight, w / innerWidth, h / innerHeight] : null;
    const next = JSON.stringify(rect);
    if (browserRect === next) return browserLayoutCall;
    browserRect = next;
    browserLayoutCall = browserLayoutCall.then(async () => {
      const result = await window.pywebview.api.action('browser_layout', { rect });
      if (!result?.ok && browserRect === next) browserRect = undefined;
    }).catch(() => { if (browserRect === next) browserRect = undefined; });
    return browserLayoutCall;
  }

  function requestBrowserLayout() {
    if (!browserLayoutFrame) browserLayoutFrame = requestAnimationFrame(() => {
      browserLayoutFrame = 0;
      syncBrowserLayout();
    });
  }

  function renderLogin() {
    const gated = loginGate(), s = ui.snapshot;
    const previouslyGated = document.body.classList.contains('login-page-active');
    document.body.classList.toggle('login-page-active', gated);
    $('login-screen').hidden = !gated;
    document.querySelector('.app-shell').hidden = gated;
    if (!gated) {
      if (previouslyGated && ui.page !== 'courses') navigate('courses');
      return;
    }
    closeMenu();
    if (!s) return;
    const status = s.state.login_status || 'idle';
    const label = s.site.label || '雨课堂';
    setText('login-site-label', label);
    setText('login-destination-name', label);
    setText('login-destination-url', s.site.url || '请选择你学校使用的雨课堂站点');
    setText('login-version', s.version ? '工作台 · v' + s.version.replace(/^v/, '') + (s.development ? ' · 开发版' : '') : '雨课堂学习工作台');
    setText('login-panel-title', status === 'waiting' ? '在浏览器中完成登录' : status === 'error' ? '重新连接，继续就好' : '连接你的学习账号');
    const defaults = { idle: '点击后打开 Edge，确认登录后自动进入课程页。', connecting: '正在打开 Edge 并检查已有登录状态…', waiting: '请在 Edge 中完成登录，这里会自动同步。', error: '暂时未能连接，请重新登录或检查站点地址。' };
    const message = s.state.login_message || defaults[status] || defaults.idle;
    setText('login-status', s.browser?.enabled ? message.replace(/Edge(?: 窗口)?/g, '内置浏览器') : message);
    setText('login-browser-step', s.browser?.enabled ? '在内置浏览器中确认账号' : '在 Edge 中确认账号');
    $('login-status').classList.toggle('is-error', status === 'error');
    syncLoginControls();
  }

  function syncLoginControls() {
    const s = ui.snapshot, status = s ? s.state.login_status || 'idle' : 'connecting';
    const busy = ui.pending || !s || status === 'connecting' || ['switching', 'closing'].includes(s.state.phase);
    $('login-button').disabled = !ui.connected || busy;
    $('login-button').setAttribute('aria-busy', String(busy));
    setText('login-button-label', ui.pending ? '正在连接…' : status === 'connecting' ? '正在连接…' : status === 'waiting' ? '返回登录窗口' : status === 'error' ? '重新登录' : '登录');
    $('login-button').classList.toggle('is-connecting', Boolean(busy));
    $('login-button').querySelector('use').setAttribute('href', '#i-' + (status === 'connecting' || ui.pending ? 'refresh' : status === 'waiting' ? 'globe' : 'user'));
    const openBrowser = $('settings-overlay').querySelector('[data-action="open_browser"]');
    openBrowser.hidden = loginGate();
  }

  function windowControlsAvailable() { return Boolean(hasAPI() && ui.snapshot && ui.snapshot.window.native_controls); }
  function renderWindowFrame() {
    const available = windowControlsAvailable(), maximized = Boolean(ui.snapshot && ui.snapshot.window.maximized);
    const closing = ui.snapshot && ui.snapshot.state.phase === 'closing';
    document.body.classList.toggle('window-is-maximized', maximized);
    document.body.classList.toggle('window-controls-connected', available);
    const desktopGlass = Boolean(available && ui.snapshot.window.desktop_glass);
    document.documentElement.classList.toggle('desktop-glass', desktopGlass || typingWindow);
    if (typingWindow) document.documentElement.classList.toggle('typing-opaque', !desktopGlass);
    $('window-maximize').querySelector('path').setAttribute('d', maximized ? 'M2 4h6v6H2zM4 4V2h6v6H8' : 'M3 1H1v2M9 1h2v2M1 9v2h2M11 9v2H9');
    const windowName = typingWindow ? '打字窗口' : '工作台';
    const labels = { 'window-close': '关闭' + windowName, 'window-minimize': '最小化' + windowName, 'window-maximize': maximized ? '还原窗口' : '最大化' + windowName };
    for (const [id, label] of Object.entries(labels)) {
      const control = $(id);
      control.disabled = !available || ui.windowPending || Boolean(closing);
      control.setAttribute('aria-label', label);
      control.title = available ? label : '窗口控制仅在桌面程序中可用';
    }
    setText('window-caption', typingWindow ? '输入工具' : available ? '桌面工作台' : window.__RAIN_DEMO__ || ui.snapshot && ui.snapshot.demo ? '浏览器演示' : '窗口控件未连接');
    $('window-drag-region').title = available ? '拖动移动窗口，双击最大化或还原' : '窗口拖动仅在桌面程序中可用';
  }
  async function sendWindowAction(name, payload = {}) {
    if (!windowControlsAvailable() || ui.windowPending) return;
    // Native window controls stay independent of learning/typing requests, so
    // a running scan cannot block Minimize or the graceful Close action.
    ui.windowPending = true; renderWindowFrame();
    try {
      const result = await window.pywebview.api.action(name, payload);
      if (result && result.ok === false) showToast(result.message || '窗口操作暂未完成。', true);
      if (name !== 'window_close') await refreshSnapshot(true);
    } catch (error) {
      if (name !== 'window_close') showToast('窗口操作暂未完成，请稍后重试。', true);
    } finally { ui.windowPending = false; renderWindowFrame(); }
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
    setText('version-label', s.version ? 'v' + s.version.replace(/^v/, '') + (s.development ? ' · 开发版' : '') : '');
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
    const pick = make('input', 'task-pick'); pick.type = 'checkbox';
    pick.addEventListener('change', () => {
      if (locked()) { renderTasks(true); return; }
      pick.checked ? selectedTasks.add(asText(task.id)) : selectedTasks.delete(asText(task.id));
      renderTasks(true);
    });
    const order = make('span', 'task-order');
    const selection = make('div', 'task-selection'); selection.append(pick, symbol, order);
    const info = make('div', 'task-info'); info.append(make('h3'), make('p'));
    const type = make('span', 'task-type');
    const status = make('div', 'task-status'); status.append(make('span', 'pill'));
    const retry = make('button', 'task-retry', '重试'); retry.type = 'button'; retry.dataset.action = 'retry_task'; retry.dataset.id = asText(task.id); status.appendChild(retry);
    const run = make('button', 'task-retry', '单独执行'); run.type = 'button'; run.dataset.action = 'run_task'; run.dataset.id = asText(task.id); status.appendChild(run);
    const progress = make('div', 'task-progress'); progress.appendChild(make('strong'));
    const track = make('div', 'progress-track'), fill = make('div', 'progress-fill'); track.append(fill); progress.appendChild(track);
    node.append(selection, info, type, status, progress);
    node._parts = { symbol, pick, order, run, title: info.firstChild, description: info.lastChild, type, status: status.firstChild, retry, progress: progress.firstChild, fill };
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
    const accountScope = fingerprint([s.site.url, s.account, s.courses.filter(c => c.selected).map(c => c.id).sort()]);
    if (accountScope !== taskAccountScope) { selectedTasks.clear(); taskAccountScope = accountScope; }
    const visible = s.tasks.filter(t => !taskHidden(t));
    const eligible = new Set(visible.filter(taskSelectable).map(t => asText(t.id)));
    if (!locked()) for (const key of selectedTasks) if (!eligible.has(key)) selectedTasks.delete(key);
    const query = ui.taskSearch.toLocaleLowerCase();
    const tasks = visible.filter(t => (taskAutomatic(t) ? 'automatic' : 'manual') === taskGroup && `${asText(t.title)} ${asText(t.course_name)}`.toLocaleLowerCase().includes(query) && matchesTaskType(t) && (!ui.unfinishedOnly || t.status !== 'completed'));
    const hash = fingerprint([tasks, locked(), ui.taskType, ui.taskSearch, ui.unfinishedOnly, s.empty.tasks, taskGroup, [...selectedTasks], visible.length]);
    if (!force && fingerprints.tasks === hash) return;
    fingerprints.tasks = hash;
    setText('task-visible-count', tasks.length + ' 项');
    setText('task-auto-count', visible.filter(taskAutomatic).length);
    setText('task-manual-count', visible.filter(t => !taskAutomatic(t)).length);
    setText('task-selection-count', `已勾选 ${selectedTasks.size} 项 · 按勾选顺序执行`);
    $('task-selection-bar').hidden = taskGroup !== 'automatic';
    $('run-selected').disabled = locked() || !selectedTasks.size;
    $('task-select-visible').disabled = locked() || !tasks.some(taskSelectable);
    $('task-clear-selected').disabled = locked() || !selectedTasks.size;
    document.querySelectorAll('[data-task-group]').forEach(b => {
      const active = b.dataset.taskGroup === taskGroup; b.classList.toggle('is-active', active); b.setAttribute('aria-selected', String(active));
    });
    const container = $('task-list'), validIds = new Set(s.tasks.map((t) => asText(t.id))), displayedIds = new Set(tasks.map((t) => asText(t.id)));
    for (const [key, node] of taskNodes) if (!validIds.has(key)) { node.remove(); taskNodes.delete(key); }
    for (const child of Array.from(container.children)) if (!displayedIds.has(child.dataset.id)) child.remove();
    tasks.forEach((task, index) => {
      const key = asText(task.id); let node = taskNodes.get(key);
      if (!node) { node = createTaskNode(task); taskNodes.set(key, node); }
      const p = node._parts, symbol = task.type === 'video' ? 'video' : task.type === 'slides' ? 'slides' : 'file';
      const selectable = taskSelectable(task);
      p.pick.hidden = taskGroup !== 'automatic'; p.pick.disabled = locked() || !selectable;
      p.pick.checked = selectedTasks.has(key); p.pick.setAttribute('aria-label', '选择任务：' + asText(task.title));
      p.order.textContent = selectedTasks.has(key) ? String([...selectedTasks].indexOf(key) + 1) : '';
      p.run.hidden = !selectable; p.run.disabled = locked();
      node.classList.toggle('is-selected', selectedTasks.has(key));
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

  const typingActive = () => Boolean(ui.snapshot && ['selecting', 'running', 'paused'].includes(ui.snapshot.typing.phase));
  const learningBusy = () => Boolean(ui.snapshot && (ui.snapshot.typing.learning_busy ?? ['running', 'scanning', 'loading_courses', 'login_required', 'switching', 'pausing', 'closing'].includes(ui.snapshot.state.phase)));
  const sourceLength = () => Array.from($('typing-source').value).length;
  function typingTargetMultiline(target) {
    if (!target) return true;
    if (typeof target.multiline === 'boolean') return target.multiline;
    return target.tag !== 'input' && !['text', 'search', 'email', 'url', 'tel', 'number', 'password', 'input'].includes(target.type);
  }
  function updateTypingSource() {
    const total = sourceLength();
    setText('typing-character-count', `${total.toLocaleString()} / 50,000 字符`);
    const target = ui.snapshot ? ui.snapshot.typing.target : null;
    const warning = total > 50000 ? '文字超过 50,000 字符，请缩短原文后再开始。' : target && !typingTargetMultiline(target) && /[\r\n]/.test($('typing-source').value) ? '当前目标为单行文本框，请先去除文字中的换行。' : '';
    $('typing-multiline-notice').hidden = !warning;
    setText('typing-multiline-notice', warning);
    syncTypingControls();
  }
  function updateTypingSpeed() {
    setText('typing-speed-value', $('typing-speed').value);
    document.querySelectorAll('[data-typing-speed]').forEach((button) => button.classList.toggle('is-active', button.dataset.typingSpeed === $('typing-speed').value));
  }
  function renderTools() {
    const s = ui.snapshot; if (!s) return;
    const t = s.typing, phase = t.phase, target = t.target && typeof t.target === 'object' ? t.target : null;
    const token = target ? asText(target.token) : '';
    // Confirmation belongs to a single browser/frame/DOM target. A newer
    // target or an interrupted run can never inherit earlier consent.
    if (token !== ui.typingTarget || ['idle', 'selecting', 'stopped', 'error'].includes(phase)) ui.confirmedTarget = '';
    ui.typingTarget = token;
    const confirmed = Boolean(token && ui.confirmedTarget === token);
    const labels = { idle: '等待选择目标', selecting: '请在网页中选择', ready: confirmed ? '目标已确认' : '请确认目标', running: '正在输出', paused: '输出已暂停', completed: '输出完成', stopped: '输出已停止', error: '需要重新选择' };
    setText('typing-state-label', labels[phase] || '等待选择目标');
    $('typing-state-label').className = 'typing-state-pill' + (phase === 'running' ? ' is-running' : phase === 'error' ? ' is-error' : phase === 'completed' ? ' is-completed' : '');
    const tag = target ? asText(target.tag || '') : '', type = target ? asText(target.type || '') : '';
    const kind = !target ? '先选择，再确认' : target.multiline === true || tag === 'textarea' || type === 'textarea' ? '多行文本框' : type === 'contenteditable' || tag === 'contenteditable' ? '富文本编辑框' : typingTargetMultiline(target) ? '网页编辑框' : '单行文本框';
    setText('typing-target-title', target ? target.label || '已选网页文本框' : '尚未指定文本框');
    setText('typing-target-kind', kind);
    setText('typing-target-page', target ? target.page_title || target.host || target.url || '当前学习网页' : '');
    setText('typing-target-url', target ? target.url || target.host || '网页地址未提供' : '');
    setText('typing-target-description', target ? [target.description || target.label || '已选文本框', target.frame ? '框架：' + asText(target.frame) : '', Number.isFinite(Number(target.characters)) ? `已有 ${number(target.characters)} 字符，将从末尾追加` : '将从末尾追加'].filter(Boolean).join(' · ') : '');
    $('typing-target-details').hidden = !target;
    $('typing-target-confirmed').hidden = !confirmed;
    $('typing-target-card').classList.toggle('is-confirmed', confirmed);
    const guidance = phase === 'selecting' ? '请切换到学习浏览器，点击指定文本框。选中后回来核对目标，也可以取消。' : phase === 'running' ? '正在向此文本框追加文字。可以调整速度、暂停或停止。' : phase === 'paused' ? '输出已暂停，继续时会核对同一个文本框和已输入内容。' : target ? confirmed ? '目标已锁定。核对原文和速度后，点击“开始输出”。' : '请核对网页地址和文本框名称，再点击“确认这个文本框”。' : ['stopped', 'error', 'completed'].includes(phase) ? '重新选择网页文本框，确认后才能再次输出。' : '点击“选择网页文本框”，然后在学习浏览器中点击你要输入的那个框。';
    setText('typing-target-guidance', guidance);
    const sent = Math.max(0, number(t.sent)), total = Math.max(0, number(t.total)), percent = total ? clamp(sent / total * 100) : 0;
    setText('typing-progress-count', `${sent.toLocaleString()} / ${total.toLocaleString()} 字符`);
    const remaining = Number.isFinite(Number(t.remaining_seconds)) ? Math.ceil(number(t.remaining_seconds)) : Math.ceil(Math.max(0, total - sent) / Math.max(1, number(t.speed, 20)));
    setText('typing-progress-remaining', phase === 'running' ? `约剩 ${remaining} 秒` : phase === 'paused' ? '已暂停 · 可继续' : phase === 'completed' ? '已完成 · 请在网页核对' : phase === 'selecting' ? '等待选择' : '等待开始');
    $('typing-progress-fill').style.width = percent + '%';
    $('typing-output-progress').setAttribute('role', 'progressbar');
    $('typing-output-progress').setAttribute('aria-valuemin', '0'); $('typing-output-progress').setAttribute('aria-valuemax', '100');
    $('typing-output-progress').setAttribute('aria-valuenow', String(Math.round(percent)));
    const defaultMessage = !t.available ? '打开学习浏览器并登录后，即可使用此工具。' : '选择并确认目标后，即可开始。';
    setText('typing-message', t.message || defaultMessage);
    const error = asText(t.error || (phase === 'error' ? t.message : ''));
    $('typing-error').hidden = !error; setText('typing-error', error);
    $('typing-busy-notice').hidden = !learningBusy();
    if (!ui.typingSpeedDirty && document.activeElement !== $('typing-speed')) { $('typing-speed').value = String(clamp(t.speed, 1, 60)); updateTypingSpeed(); }
    $('typing-step-pick').classList.toggle('is-active', !target);
    $('typing-step-pick').classList.toggle('is-done', Boolean(target));
    $('typing-step-confirm').classList.toggle('is-active', Boolean(target && !confirmed && ['ready', 'completed'].includes(phase)));
    $('typing-step-confirm').classList.toggle('is-done', confirmed || ['running', 'paused'].includes(phase));
    $('typing-step-start').classList.toggle('is-active', confirmed || ['running', 'paused'].includes(phase));
    updateTypingSource();
  }
  function syncTypingControls() {
    const s = ui.snapshot, t = s ? s.typing : { phase: 'idle', available: false }, phase = t.phase;
    const target = t.target, token = target ? asText(target.token) : '', confirmed = Boolean(token && ui.confirmedTarget === token);
    const active = ['running', 'paused'].includes(phase), selecting = phase === 'selecting', blocked = learningBusy() || ui.pending || !ui.connected;
    const canSelect = t.can_select ?? (t.available && !active && !selecting);
    const sourceOK = sourceLength() > 0 && sourceLength() <= 50000 && (!target || typingTargetMultiline(target) || !/[\r\n]/.test($('typing-source').value));
    $('typing-pick').hidden = selecting || active;
    $('typing-pick').disabled = blocked || !canSelect;
    $('typing-pick').lastChild.textContent = target ? '重新选择文本框' : '选择网页文本框';
    $('typing-cancel-pick').hidden = !selecting; $('typing-cancel-pick').disabled = ui.pending;
    $('typing-confirm').hidden = !['ready', 'completed'].includes(phase) || !token || confirmed; $('typing-confirm').disabled = blocked;
    $('typing-start').hidden = active || selecting;
    $('typing-start').disabled = blocked || !t.available || !['ready', 'completed'].includes(phase) || !confirmed || !sourceOK || t.can_start === false;
    $('typing-start').lastChild.textContent = phase === 'completed' ? '输入下一段' : '开始输入';
    $('typing-pause').hidden = phase !== 'running'; $('typing-pause').disabled = ui.pending || t.can_pause === false;
    $('typing-resume').hidden = phase !== 'paused'; $('typing-resume').disabled = ui.pending || !token || t.can_resume === false;
    $('typing-stop').hidden = !active && !token; $('typing-stop').disabled = ui.pending || t.can_stop === false;
    $('typing-stop').lastChild.textContent = active ? '停止输入' : '解除锁定';
    $('typing-source').readOnly = active;
    $('typing-clear-source').disabled = active || !sourceLength();
    $('typing-speed').disabled = !t.available || selecting || ui.pending;
    document.querySelectorAll('[data-typing-speed]').forEach((button) => { button.disabled = $('typing-speed').disabled; });
    document.querySelector('[data-action="typing_pause_learning"]').disabled = ui.pending || !s || ['pausing', 'switching', 'closing'].includes(s.state.phase);
  }

  async function handleTypingAction(action) {
    const t = ui.snapshot ? ui.snapshot.typing : {}, target = t.target || {}, token = asText(target.token);
    if (action === 'typing_clear_source') { if (!['running', 'paused'].includes(t.phase)) { $('typing-source').value = ''; updateTypingSource(); } return; }
    if (action === 'typing_confirm') { if (['ready', 'completed'].includes(t.phase) && token && !learningBusy()) { ui.confirmedTarget = token; renderTools(); } return; }
    if (action === 'typing_pause_learning') { await sendAction(typingWindow ? 'pause' : 'primary'); return; }
    if (action === 'typing_pick') { ui.confirmedTarget = ''; await showTypingBrowser(); await sendAction('typing_select'); return; }
    if (action === 'typing_cancel_pick') { ui.confirmedTarget = ''; await sendAction('typing_cancel'); return; }
    if (action === 'typing_start') {
      if (!['ready', 'completed'].includes(t.phase) || !token || ui.confirmedTarget !== token || learningBusy()) { showToast('请先选择并确认指定文本框。', true); return; }
      const text = $('typing-source').value;
      if (!text || sourceLength() > 50000) { showToast('请输入 1–50,000 字符的文字。', true); return; }
      if (!typingTargetMultiline(target) && /[\r\n]/.test(text)) { showToast('单行文本框不支持换行，请修改原文。', true); return; }
      ui.typingSpeedDirty = false;
      await showTypingBrowser();
      await sendAction('typing_start', { token, text, speed: number($('typing-speed').value, 20), confirmed: true }); return;
    }
    if (action === 'typing_resume') { if (t.phase === 'paused' && token) { await showTypingBrowser(); await sendAction('typing_resume', { token }); } return; }
    if (action === 'typing_stop') { ui.confirmedTarget = ''; await sendAction('typing_stop'); return; }
    if (action === 'typing_pause') await sendAction('typing_pause');
  }

  async function showTypingBrowser() {
    if (ui.snapshot?.browser?.enabled) {
      navigate('browser', true);
      await syncBrowserLayout();
    }
  }

  function syncDisabled() {
    if (typingWindow) { syncTypingControls(); return; }
    renderAssessment();
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
    const siteBusyMessage = '请先暂停任务，等待当前操作停止后切换站点。';
    if (!$('settings-overlay').hidden && s && (s.state.selection_locked ?? s.state.busy)) setText('site-error', siteBusyMessage);
    else if ($('site-error').textContent === siteBusyMessage) setText('site-error', '');
    if (typingActive()) $('primary-button').disabled = true;
    if (!s?.separate_typing) syncTypingControls();
    syncLoginControls();
    renderUpdate();
  }

  function renderUpdate() {
    const s = ui.snapshot, update = s && s.update || {}, phase = update.phase || 'idle';
    const working = ['checking', 'downloading', 'installing'].includes(phase);
    setText('update-current', s && s.version ? '当前 v' + s.version : '正在读取版本');
    setText('update-latest', update.latest ? '正式版 v' + update.latest : 'GitHub 正式版');
    setText('update-status', update.message || '点击检查更新，查看最新正式版');
    setText('update-notes', update.notes || '');
    $('update-details').hidden = !update.notes;
    $('update-check').disabled = ui.pending || !ui.connected || working;
    setText('update-check', phase === 'checking' ? '正在检查…' : '检查更新');
    $('update-download').hidden = !update.can_download;
    $('update-download').disabled = ui.pending || working;
    $('update-cancel').hidden = !['checking', 'downloading'].includes(phase);
    $('update-cancel').disabled = ui.pending;
    $('update-install').hidden = phase !== 'ready';
    $('update-install').disabled = ui.pending || !update.install_supported || !update.can_install;
    $('update-release').disabled = ui.pending || !ui.connected || phase === 'installing';
    $('update-progress-area').hidden = phase !== 'downloading';
    const total = Number(update.total) || 0, downloaded = Number(update.downloaded) || 0;
    $('update-progress').value = total ? Math.min(100, downloaded * 100 / total) : 0;
    const size = (bytes) => (bytes / 1048576).toFixed(1) + ' MB';
    setText('update-progress-label', size(downloaded) + ' / ' + size(total));
    const hint = phase === 'ready' && !update.install_supported
      ? '源码 / 演示模式不执行覆盖安装；请在 Windows 安装版中使用。'
      : phase === 'ready' && !update.can_install
        ? '请先暂停学习并结束手动打字，等待当前操作停止后安装。'
        : '安装前保存进度并退出工作台，覆盖当前目录；保留登录状态、配置与任务记录。';
    setText('update-hint', hint);
  }

  function navigate(page, immediate = false) {
    if (loginGate()) return;
    if (!pageNames[page] || page === ui.page) return;
    const oldPage = $('page-' + ui.page), nextPage = $('page-' + page), token = ++ui.navigationToken;
    ui.page = page;
    // Hidden pages do not consume DOM work for every background snapshot.
    // Materialize their latest display data only when the user opens them.
    if (page === 'courses') renderCourses();
    else if (page === 'tasks') renderTasks();
    else if (page === 'activity') renderActivity();
    else if (!ui.snapshot?.separate_typing && page === 'tools') renderTools();
    document.body.classList.toggle('tools-page-active', page === 'tools');
    document.body.classList.toggle('browser-page-active', page === 'browser');
    syncDisabled();
    document.querySelectorAll('[data-page]').forEach((button) => { const active = button.dataset.page === page; button.classList.toggle('is-active', active); if (active) button.setAttribute('aria-current', 'page'); else button.removeAttribute('aria-current'); });
    setText('page-breadcrumb', pageNames[page]);
    closeMenu();
    oldPage.classList.add('is-leaving');
    const showPage = () => {
      if (token !== ui.navigationToken) return;
      document.querySelectorAll('.page-panel').forEach((panel) => { panel.hidden = panel !== nextPage; panel.classList.remove('is-leaving', 'is-active'); });
      nextPage.classList.add('is-active');
      if (page === 'browser') document.querySelector('.main-content').scrollTop = 0;
      requestBrowserLayout();
    };
    if (immediate || page === 'browser' || oldPage.id === 'page-browser') showPage();
    else window.setTimeout(showPage, reduceMotion.matches ? 0 : 140);
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
    ui.pending = true; syncDisabled();
    if (!typingWindow) { renderCourses(); renderTasks(); }
    const startTime = performance.now();
    try {
      const result = hasAPI() ? await window.pywebview.api.action(name, payload) : window.__RAIN_DEMO__ ? await demoAction(name, payload) : { ok: false, message: '桌面连接暂不可用，请重新打开程序。' };
      if (result && result.navigate) navigate(result.navigate);
      if (name === 'switch_site' && result && result.ok) closeSettings();
      if (result && result.message) showToast(result.message, result.ok === false);
      if (result && result.ok === false && name === 'switch_site') setText('site-error', result.message || '站点切换未成功');
      await refreshSnapshot(true);
      return result;
    } catch (error) {
      showToast('操作暂未成功，请稍后重试；详细信息可查看运行记录。', true);
    } finally {
      // A short double-click guard outlives fast local commands, while business
      // state in the next snapshot determines whether pause is immediately usable.
      const remaining = Math.max(0, 280 - (performance.now() - startTime));
      if (remaining) await new Promise((resolve) => setTimeout(resolve, remaining));
      ui.pending = false; syncDisabled();
      if (!typingWindow) { renderCourses(); renderTasks(); }
    }
  }

  async function handleAction(action, target) {
    if (action === 'assessment_toggle') {
      const panel = $('assessment-panel');
      const launch = $('browser-assessment-tools');
      panel.hidden = !panel.hidden;
      launch.setAttribute('aria-expanded', String(!panel.hidden));
      if (panel.hidden) {
        $('assessment-key').value = '';
        launch.focus();
      }
      renderAssessment();
      requestBrowserLayout();
      if (!panel.hidden) await sendAction('assessment_console');
      return;
    }
    if (action === 'assessment_key_settings') {
      assessmentKeySettings = !assessmentKeySettings;
      renderAssessment();
      if (!$('assessment-key-form').hidden) $('assessment-key').focus();
      return;
    }
    if (action === 'assessment_tab_chat' || action === 'assessment_tab_tasks') {
      assessmentTab = action === 'assessment_tab_chat' ? 'chat' : 'tasks';
      renderAssessment();
      return;
    }
    if (action === 'assessment_select_task') {
      assessmentFeedback = '正在进入所选任务…';
      renderAssessment();
      const result = await sendAction(action, { ...assessmentOptions(), id: target.dataset.id });
      assessmentFeedback = result?.ok ? '' : result?.message || '任务未能启动，请稍后重试。';
      if (result?.ok) assessmentTab = 'chat';
      renderAssessment();
      return;
    }
    if (action === 'assessment_run_current') {
      const result = await sendAction(action, assessmentOptions());
      assessmentFeedback = result?.ok ? '' : result?.message || '操作未能启动，请重试。';
      if (result?.ok) {
        assessmentTab = 'chat';
      }
      renderAssessment();
      return;
    }
    if (action === 'browser_typing_tools') {
      if (ui.snapshot?.separate_typing) await sendAction('open_typing_window');
      else navigate('tools');
      return;
    }
    if (['run_selected', 'run_task', 'task_select_visible', 'task_clear_selected'].includes(action)) {
      if (locked() || !ui.snapshot) return;
      if (action === 'task_clear_selected') { selectedTasks.clear(); renderTasks(true); return; }
      if (action === 'task_select_visible') {
        const query = ui.taskSearch.toLocaleLowerCase();
        ui.snapshot.tasks.filter(t => !taskHidden(t) && taskSelectable(t) && matchesTaskType(t) &&
          `${asText(t.title)} ${asText(t.course_name)}`.toLocaleLowerCase().includes(query)).forEach(t => selectedTasks.add(asText(t.id)));
        renderTasks(true); return;
      }
      await sendAction('start_tasks', { ids: action === 'run_task' ? [target.dataset.id] : [...selectedTasks] }); return;
    }
    if (action.startsWith('window_')) { await sendWindowAction(action); return; }
    if (action.startsWith('typing_')) { await handleTypingAction(action); return; }
    if (action === 'settings') { openSettings(); return; }
    if (action === 'close_settings') { closeSettings(); return; }
    if (action === 'navigate_courses') { navigate('courses'); return; }
    closeMenu();
    const names = { select_current: ['select_courses', { mode: 'current' }], select_all: ['select_courses', { mode: 'all' }], select_clear: ['select_courses', { mode: 'clear' }], retry_task: ['retry_tasks', { ids: [target.dataset.id] }], retry_all: ['retry_tasks', { ids: ui.snapshot ? ui.snapshot.tasks.filter((task) => task.status === 'failed').map((task) => asText(task.id)) : [] }] };
    if (action === 'retry_all' && !names.retry_all[1].ids.length) { showToast('当前没有需要重试的失败任务。'); return; }
    const command = names[action] || [action, {}]; await sendAction(...command);
  }

  document.addEventListener('click', (event) => {
    const tab = event.target.closest('[data-task-group]');
    if (tab) { taskGroup = tab.dataset.taskGroup; ui.taskType = 'all'; $('task-type').value = 'all'; renderTasks(true); return; }
    const speedPreset = event.target.closest('[data-typing-speed]');
    if (speedPreset && !speedPreset.disabled) { $('typing-speed').value = speedPreset.dataset.typingSpeed; ui.typingSpeedDirty = true; updateTypingSpeed(); if (ui.snapshot && ['running', 'paused'].includes(ui.snapshot.typing.phase)) sendAction('typing_speed', { speed: number($('typing-speed').value, 20) }); return; }
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
  $('browser-address-form').addEventListener('submit', event => {
    event.preventDefault();
    if (!$('browser-go').disabled) sendAction('browser_command', { command: 'go', url: $('browser-address').value.trim() });
  });
  $('assessment-key-form').addEventListener('submit', async event => {
    event.preventDefault();
    if ($('assessment-save-key').disabled) return;
    const field = $('assessment-key'), api_key = field.value.trim();
    if (!api_key) { field.focus(); return; }
    field.value = '';
    if ((await sendAction('assessment_save_key', { api_key }))?.ok) {
      assessmentKeySettings = false;
      renderAssessment();
      $('assessment-chat-input').focus();
    }
  });
  $('assessment-chat-form').addEventListener('submit', async event => {
    event.preventDefault();
    const input = $('assessment-chat-input'), text = input.value.trim();
    if (!text || $('assessment-send').disabled) return;
    const readPage = $('assessment-read-page');
    const result = await sendAction('assessment_chat', { ...assessmentOptions(), text, read_page: readPage.checked && !readPage.disabled });
    if (result?.ok) input.value = '';
  });
  $('assessment-chat-input').addEventListener('keydown', event => {
    if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) {
      event.preventDefault();
      $('assessment-chat-form').requestSubmit();
    }
  });
  const modelControl = $('assessment-model-control');
  const modelItems = [...modelControl.querySelectorAll('[data-assessment-model]')];
  $('assessment-model-toggle').addEventListener('click', () => {
    setAssessmentModelOpen($('assessment-model-menu').hidden);
  });
  modelItems.forEach(item => item.addEventListener('click', () => {
    if ($('assessment-model-toggle').disabled) return;
    $('assessment-model').value = item.dataset.assessmentModel;
    const label = item.querySelector('strong').textContent;
    setText('assessment-model-current', label);
    $('assessment-model-toggle').setAttribute('aria-label', '选择模型：' + label);
    modelItems.forEach(option => option.setAttribute('aria-checked', String(option === item)));
    if ($('assessment-model').value !== 'deepseek-v4-pro') $('assessment-web').checked = false;
    setAssessmentModelOpen(false, true);
    renderAssessment();
  }));
  modelControl.addEventListener('keydown', event => {
    const open = !$('assessment-model-menu').hidden;
    if (event.key === 'Escape' && open) {
      event.preventDefault(); event.stopPropagation(); setAssessmentModelOpen(false, true);
    } else if (['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(event.key)) {
      event.preventDefault();
      if (!open) { setAssessmentModelOpen(true); return; }
      const index = modelItems.indexOf(document.activeElement);
      const next = event.key === 'Home' ? 0 : event.key === 'End' ? modelItems.length - 1 :
        (index + (event.key === 'ArrowDown' ? 1 : -1) + modelItems.length) % modelItems.length;
      modelItems[next].focus();
    } else if (event.key === 'Tab' && open) {
      setAssessmentModelOpen(false, true);
    }
  });
  modelControl.addEventListener('focusout', event => {
    if (!modelControl.contains(event.relatedTarget)) setAssessmentModelOpen(false);
  });
  document.addEventListener('pointerdown', event => {
    if (!modelControl.contains(event.target)) setAssessmentModelOpen(false);
  });
  const effortControl = $('assessment-effort-control');
  $('assessment-effort-toggle').addEventListener('click', () => {
    setAssessmentEffortOpen($('assessment-effort-popover').hidden);
  });
  $('assessment-effort').addEventListener('input', event => {
    const label = assessmentEffortLabels[event.target.valueAsNumber];
    setText('assessment-effort-current', label);
    setText('assessment-effort-name', label);
    setText('assessment-effort-note', ['快速回应，适合简单对话', '轻量分析，兼顾响应速度', '复杂问题，深入核对', '投入更多推理，响应可能更慢'][event.target.valueAsNumber]);
    $('assessment-effort-popover').style.setProperty('--effort-fill', `${event.target.valueAsNumber / 3 * 100}%`);
    $('assessment-effort-popover').classList.toggle('is-max', event.target.valueAsNumber === 3);
    event.target.setAttribute('aria-valuetext', label);
    $('assessment-effort-toggle').setAttribute('aria-label', '推理强度：' + label);
  });
  effortControl.addEventListener('keydown', event => {
    if (event.key !== 'Escape' || $('assessment-effort-popover').hidden) return;
    event.preventDefault();
    event.stopPropagation();
    setAssessmentEffortOpen(false, true);
  });
  effortControl.addEventListener('focusout', event => {
    if (!effortControl.contains(event.relatedTarget)) setAssessmentEffortOpen(false);
  });
  document.addEventListener('pointerdown', event => {
    if (!effortControl.contains(event.target)) setAssessmentEffortOpen(false);
  });
  document.querySelectorAll('[data-browser-command]').forEach(button => button.addEventListener('click', () => {
    if (!button.disabled) sendAction('browser_command', { command: button.dataset.browserCommand });
  }));
  new ResizeObserver(requestBrowserLayout).observe($('browser-viewport'));
  new MutationObserver(requestBrowserLayout).observe($('settings-overlay'), { attributes: true, attributeFilter: ['hidden'] });
  window.addEventListener('resize', requestBrowserLayout);
  document.addEventListener('scroll', requestBrowserLayout, true);
  $('window-titlebar').addEventListener('dblclick', (event) => {
    if (event.target.closest('button,input,textarea,select,a,[contenteditable]')) return;
    if (event.target.closest('.pywebview-drag-region')) { event.preventDefault(); sendWindowAction('window_toggle_maximize'); }
  });
  document.addEventListener('mousedown', (event) => {
    const edge = event.target.closest('[data-window-resize]');
    if (!edge || event.button !== 0 || !windowControlsAvailable() || ui.snapshot.window.maximized) return;
    event.preventDefault(); sendWindowAction('window_resize', { edge: edge.dataset.windowResize });
  });
  $('more-trigger').addEventListener('click', () => { const open = $('more-menu').hidden; $('more-menu').hidden = !open; $('more-trigger').setAttribute('aria-expanded', String(open)); });
  document.querySelector('.brand').addEventListener('click', (event) => { event.preventDefault(); navigate('courses'); });
  $('course-search').addEventListener('input', (event) => { ui.courseSearch = event.target.value; renderCourses(); });
  $('task-search').addEventListener('input', (event) => { ui.taskSearch = event.target.value; renderTasks(); });
  $('typing-source').addEventListener('input', updateTypingSource);
  $('typing-speed').addEventListener('input', () => { ui.typingSpeedDirty = true; updateTypingSpeed(); });
  $('typing-speed').addEventListener('change', () => { if (ui.snapshot && ['running', 'paused'].includes(ui.snapshot.typing.phase)) sendAction('typing_speed', { speed: number($('typing-speed').value, 20) }); });
  const taskFilterChanged = () => { ui.taskType = $('task-type').value; ui.unfinishedOnly = $('unfinished-only').checked; renderTasks(); if (ui.connected && !ui.pending) sendAction('task_filter', { type: ui.taskType, unfinished_only: ui.unfinishedOnly }); };
  $('task-type').addEventListener('change', taskFilterChanged); $('unfinished-only').addEventListener('change', taskFilterChanged);
  $('site-form').addEventListener('submit', (event) => { event.preventDefault(); if (locked()) return; const url = $('site-url').value.trim(); if (!url) { setText('site-error', '请输入学校使用的雨课堂主页地址。'); return; } sendAction('switch_site', { url }); });
  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape') {
      // Close a foreground layer first. Escape never edits or clears the local
      // draft, and only active typing operations receive the stop shortcut.
      if (!$('settings-overlay').hidden) { event.preventDefault(); closeSettings(); return; }
      if (!$('more-menu').hidden) { event.preventDefault(); closeMenu(); return; }
      if ((typingWindow || ui.page === 'tools') && ui.snapshot) {
        const phase = ui.snapshot.typing.phase;
        if (phase === 'selecting') { event.preventDefault(); handleTypingAction('typing_cancel_pick'); return; }
        if (['running', 'paused'].includes(phase)) { event.preventDefault(); handleTypingAction('typing_stop'); return; }
      }
    }
    if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'f' && !loginGate()) { event.preventDefault(); if (ui.page === 'courses') $('course-search').focus(); else if (ui.page === 'tasks') $('task-search').focus(); }
    // Keep modal keyboard navigation inside its dialog; restore focus on close.
    if (event.key === 'Tab' && !$('settings-overlay').hidden) {
      const items = Array.from($('settings-overlay').querySelectorAll('button:not(:disabled), input:not(:disabled), summary, [tabindex="0"]')).filter((node) => node.getClientRects().length);
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
        else if (!ui.connected && !typingWindow) { setText('hero-title', '正在连接桌面工作台。'); setText('hero-description', '连接完成后，可选择课程并开始处理任务。'); }
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
    return { demo: true, version: '0.8.7', site: { label: '长江雨课堂', url: 'https://changjiang.yuketang.cn/web', history: [{ label: '雨课堂', url: 'https://www.yuketang.cn/v2/web' }, { label: '长江雨课堂', url: 'https://changjiang.yuketang.cn/web' }] }, account: { name: '演示同学', logged_in: true }, state: { phase: 'idle', busy: false, selection_locked: false, message: '课程选择已保存。直接点击开始，自动扫描并安排学习任务。', primary: { label: '开始学习', action: 'start', enabled: true } }, counts: { courses: courses.length, selected: 3, tasks: tasks.length, completed: 1, pending: 3, errors: 0 }, courses, tasks, logs: [{ time: '10-13 15:24:12', level: '服务器已确认', course: '艺术鉴赏与生活美学', task: '如何欣赏一幅画', message: '任务完成状态已同步。' }, { time: '10-13 15:23:48', level: '扫描完成', message: '所选课程任务已加入队列。' }], scan_errors: [], progress: { current: 1, total: 5, percent: 20, label: '学习进度' }, filters: { task_type: 'all', unfinished_only: true } };
  }
  async function demoAction(name, payload) {
    if (name.startsWith('typing_')) return { ok: false, message: '演示工具不会向网页输出。请在正式程序中选择真实目标。' };
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
  if (typingWindow) {
    ui.page = 'tools';
    document.querySelector('.app-shell').hidden = false;
    $('page-courses').hidden = true;
    $('page-tools').hidden = false;
    $('page-tools').classList.add('is-active');
    $('login-screen').hidden = true;
    const dock = make('footer', 'typing-window-dock');
    dock.append(document.querySelector('.typing-bottom-note'), document.querySelector('.typing-run-actions'));
    document.querySelector('.app-shell').appendChild(dock);
    syncTypingControls();
  }
  renderWindowFrame();
  poll();
})();
