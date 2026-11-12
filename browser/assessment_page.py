"""Bounded assessment DOM readers and a writer for known native controls."""
import json
import re
import time
from browser.assessment_route import EXAM_ORIGIN


_DOM_SCRIPT = r"""
(() => {
  if (location.origin !== request.origin) return {error: '请在当前雨课堂站点内打开题页。'};
  if (document.readyState === 'loading') return {error: '题页还在加载，请稍后重试。'};
  // DOM references are local to this document. A replacement node receives a
  // new identifier even when its visible text and position look the same.
  if (!Object.hasOwn(window, request.registry)) {
    let count = 0;
    const ids = new WeakMap();
    Object.defineProperty(window, request.registry, {value: node => {
      if (!ids.has(node)) ids.set(node, String(++count));
      return ids.get(node);
    }});
  }
  const nodeId = window[request.registry];
  const visible = el => el.getClientRects().length &&
    (el.checkVisibility ? el.checkVisibility({visibilityProperty: true, opacityProperty: true}) :
      !['hidden', 'collapse'].includes(el.ownerDocument.defaultView.getComputedStyle(el).visibility));
  const excluded = 'input, textarea, select, [contenteditable], script, style, template, noscript, iframe, [aria-hidden=true]';
  const stripQueries = value => value.replace(/(?:https?:\/\/|www\.)[^\s<>"'，。；）】]+/giu, url => url.split(/[?#]/, 1)[0])
    .replace(/(?:[./][^\s<>"'，。；）】]*)[?#][^\s<>"'，。；）】]*/gu, '[链接]')
    .replace(/[?#][\w.%+-]+=[^\s<>"'，。；）】]*/gu, '[链接]');
  const cleanText = value => stripQueries(value).replace(/\s+/gu, ' ').trim();
  const text = (el, limit, excludeLabels = false) => {
    if (!el || el.closest(excluded)) return '';
    const walker = el.ownerDocument.createTreeWalker(el, 4);
    const parts = [];
    let length = 0;
    for (let n = 0, node; n < 4000 && (node = walker.nextNode()); n++) {
      const parent = node.parentElement;
      if (!parent || parent.closest(excluded) || (excludeLabels && parent.closest('label')) || !visible(parent)) continue;
      const link = parent.closest('a[href]');
      if (link && /[?#]\s*[\w.%+-]+\s*=/u.test((link.textContent || '').slice(0, 1000))) continue;
      const value = cleanText(node.data.slice(0, limit + 100));
      if (value) {
        parts.push(value);
        length += value.length + 1;
        if (length > limit + 1) break;
      }
    }
    return parts.join(' ').slice(0, limit + 1);
  };
  const stemText = el => el && !el.closest(excluded) && !el.querySelector(excluded + ', a[href]') &&
    typeof el.innerText === 'string' ? stripQueries(el.innerText.slice(0, 4100)).trim().slice(0, 4001) : text(el, 4000);
  const media = el => Boolean(el?.querySelector('img, svg, canvas, math, video, audio'));
  const questionsSelector = '[data-question-id], fieldset' + (request.exam ? ', section.subject-item' : '');
  const stemsSelector = '[data-question-stem], [data-role="question-stem"], .question-stem, .question-title, legend' +
    (request.exam ? ', .item-body > h4.exam-font' : '');
  const formSelector = 'input:not([type=hidden]), textarea, select, [contenteditable=true], [role=radio], [role=checkbox], [role=textbox]';
  const buttonSelector = 'button, [role=button], input[type=button], input[type=submit], input[type=reset], input[type=image]';
  const actionSelector = buttonSelector + ', a[href]';
  const label = el => [...(el.labels || [])].find(visible);
  const shown = el => visible(el) || Boolean(label(el));
  const kindOf = el => {
    if (el.tagName === 'TEXTAREA') return 'textarea';
    if (el.tagName === 'INPUT') return el.type;
    return el.getAttribute('role') || el.tagName.toLowerCase();
  };
  const available = el => el.isConnected && shown(el) && !el.matches(':disabled, [readonly]') &&
    !el.closest('[inert], [aria-disabled=true], [aria-hidden=true]');
  const answerControls = root => [...root.querySelectorAll(formSelector)].filter(el =>
    shown(el) && !['button', 'submit', 'reset', 'image'].includes(kindOf(el)) &&
    !(el.matches('[role=radio], [role=checkbox]') && el.querySelector('input[type=radio], input[type=checkbox]')));
  const docs = [document];
  let unreadableFrames = 0;
  for (const doc of docs) {
    for (const frame of doc.querySelectorAll('iframe')) {
      if (!visible(frame)) continue;
      const child = frame.contentDocument;
      if (child?.body && docs.length < 16 &&
          (child.location.origin === request.origin || ['about:blank', 'about:srcdoc'].includes(child.URL))) {
        docs.push(child);
      } else {
        unreadableFrames++;
      }
    }
  }

  const controlId = el => 'c' + nodeId(el);
  const groupId = el => 'g' + nodeId(el);
  const groupContext = (el, limit) => text(el, limit).slice(0, limit);
  const contextCache = new WeakMap();
  const labelText = el => text(label(el), 1000);
  const controlLabel = el => (labelText(el) || cleanText((el.getAttribute('aria-label') || '').slice(0, 400)) ||
    text(el, 180)).slice(0, 180);
  const groupsFor = el => {
    const groups = [];
    for (let root = el.parentElement, depth = 0; root && depth < 6 && groups.length < 3;
         root = root.parentElement, depth++) {
      if (root.matches('html, body, form') || root.querySelector(buttonSelector)) break;
      if (!contextCache.has(root)) contextCache.set(root, groupContext(root, 320));
      const context = contextCache.get(root);
      if (context && (!root.matches('label') || ['text', 'textarea'].includes(kindOf(el)))) {
        groups.push({id: groupId(root), text: context});
      }
    }
    return groups;
  };

  const observe = () => {
    const controls = [];
    const pages = [];
    for (const doc of docs) {
      pages.push(groupContext(doc.body, 4200));
      for (const el of doc.querySelectorAll(formSelector + ', ' + actionSelector)) {
        if (!shown(el)) continue;
        const type = kindOf(el);
        const groups = ['radio', 'checkbox', 'text', 'textarea'].includes(type) ? groupsFor(el) : [];
        controls.push({id: controlId(el), type, label: controlLabel(el), groups});
        if (controls.length === 60) break;
      }
      if (controls.length === 60) break;
    }
    return {title: cleanText(document.title.slice(0, 480)).slice(0, 180),
      text: pages.join('\n').slice(0, 6000), controls};
  };

  const radioGroup = el => [...el.getRootNode().querySelectorAll('input[type=radio]')].filter(other =>
    other.name === el.name && other.form === el.form);
  const sameControls = (left, right) =>
    left.length === right.length && left.every(control => right.includes(control));
  const examRadioComponents = (root, controls) => {
    if (!request.exam || !root.matches('section.subject-item') || controls.length < 2) return null;
    const radios = controls.map(el => {
      const wrapper = el.closest('label.el-radio[role=radio]');
      const vm = wrapper?.__vue__;
      return vm?.$options?.name === 'ElRadio' && vm.$el === wrapper &&
        root.contains(vm.$parent?.$el) && ['string','number'].includes(typeof vm.label) ? vm : null;
    });
    return radios.every(vm => vm && vm.$parent === radios[0].$parent) &&
      new Set(radios.map(vm => vm.label)).size === radios.length ? radios : null;
  };
  const classify = (root, stem, stemNode, controls, id, adapted = false) => {
    const options = [];
    const issues = [];
    let kind = '';
    const type = controls[0] && kindOf(controls[0]);
    let examRadios = null;
    if (['radio', 'checkbox'].includes(type) && controls.every(el => el.tagName === 'INPUT' && el.type === type)) {
      kind = type === 'radio' ? 'single' : 'multiple';
      if (kind === 'single') examRadios = examRadioComponents(root, controls);
      if (type === 'radio' && !examRadios && (!controls[0].name || !sameControls(controls, radioGroup(controls[0])))) {
        issues.push('单选组跨题或不完整');
      }
      if (controls.length > 20) issues.push('选项数量超出读取范围');
      for (const el of controls.slice(0, 20)) {
        const option = labelText(el) || (adapted ? cleanText((el.getAttribute('aria-label') || '').slice(0, 1001)) : '');
        if (!option || media(label(el))) issues.push('选项缺少可读文字或包含图片');
        options.push({id: String(options.length + 1), text: option});
      }
    } else if (controls.length === 1 && (controls[0].tagName === 'TEXTAREA' ||
               (controls[0].tagName === 'INPUT' && type === 'text'))) {
      kind = 'text';
    } else {
      issues.push('题型或控件尚未适配');
    }
    if (media(stemNode) || root.querySelector('img, canvas, math, video, audio')) {
      issues.push('需要核对图片、公式或其他媒体');
    }
    const question = {id, kind, stem, options, issues};
    if (request.exam && root.matches('section.subject-item')) {
      const passage = [...root.parentElement.children].find(el => el.matches('.dict_description') && visible(el));
      if (passage) {
        question.material = text(passage, 12000);
        if (!question.material || media(passage)) issues.push('共享阅读材料需要核对');
      }
    }
    return {question, controls, root, stemNode, adapted, examRadios,
      identity: [groupId(root), stemNode && nodeId(stemNode), controls.map(controlId)]};
  };

  const adapt = proposals => {
    const candidates = new Map();
    for (const doc of docs) {
      for (const el of doc.querySelectorAll(formSelector)) {
        if (shown(el) && ['radio', 'checkbox', 'text', 'textarea'].includes(kindOf(el))) {
          candidates.set(controlId(el), el);
        }
      }
    }
    const adopted = [];
    const used = new Set();
    for (const item of proposals) {
      const selected = item.controls.map(id => candidates.get(id));
      if (selected.some(el => !el || used.has(el))) return null;
      let root = selected[0].parentElement;
      while (root && groupId(root) !== item.group) root = root.parentElement;
      if (!root || !groupsFor(selected[0]).some(group => group.id === item.group && group.text === item.context)) return null;
      const controls = answerControls(root);
      if (!sameControls(selected, controls)) return null;
      if (!selected.every((el, index) => controlLabel(el) === item.labels[index])) return null;
      const result = classify(root, item.stem, null, controls, String(adopted.length + 1), true);
      if (!result.question.kind) return null;
      if (result.question.kind !== 'text' && !text(root, 320, true).includes(item.stem)) return null;
      adopted.push(result);
      controls.forEach(el => used.add(el));
    }
    return adopted;
  };

  const scan = proposals => {
    if (proposals) {
      const rows = adapt(proposals);
      return rows && {rows, truncated: false};
    }
    const rows = [];
    for (const doc of docs) {
      for (const root of doc.querySelectorAll(questionsSelector)) {
        if (!visible(root) || root.parentElement?.closest(questionsSelector)) continue;
        const stem = [...root.querySelectorAll(stemsSelector)].find(visible);
        if (!stem && !root.hasAttribute('data-question-id')) continue;
        const controls = answerControls(root);
        if (request.exam && root.matches('section.subject-item') && !controls.length) continue;
        if (rows.length === 80) return {rows, truncated: true};
        rows.push(classify(root, stemText(stem), stem || null, controls, String(rows.length + 1)));
      }
    }
    return {rows, truncated: false};
  };
  const snapshot = result => ({
    questions: result.rows.map(row => row.question), identities: result.rows.map(row => row.identity),
    truncated: result.truncated, unreadableFrames,
  });

  if (request.mode === 'surface') return observe();
  if (request.mode === 'fill' && Date.now() > request.deadline) return {error: '填写请求已超时，请重新确认。'};
  const current = scan(request.proposals);
  if (!current) return {error: '题页布局已变化或控件分组无效，请重新观察。'};
  const actual = snapshot(current);
  if (request.mode === 'capture') return actual;
  const canonical = value => {
    if (Array.isArray(value)) return value.map(canonical);
    if (value && typeof value === 'object') {
      return Object.fromEntries(Object.keys(value).sort().map(key => [key, canonical(value[key])]));
    }
    return value;
  };
  if (JSON.stringify(canonical(actual)) !== JSON.stringify(canonical(request.expected))) {
    return {error: '题页内容或答题控件已变化，请重新读取。'};
  }
  if (request.mode === 'verify') {
    if (request.complete) {
      const bound = new Set(current.rows.flatMap(row => row.controls));
      const omitted = docs.some(doc => answerControls(doc.body).some(el => !bound.has(el) &&
        !el.matches('input[type=search]') && !el.closest('[role=search], [role=dialog], dialog, .el-message-box, .el-dialog')));
      const hidden = docs.some(doc => [...doc.querySelectorAll(questionsSelector)].some(el =>
        !visible(el) && el.querySelector(formSelector)));
      if (omitted || hidden || actual.truncated || actual.unreadableFrames) {
        return {error: '页面还有未纳入读取范围的答题控件或隐藏题目，不能提交。'};
      }
    }
    const filled = current.rows.filter(row => {
      const answer = request.answers[row.question.id];
      if (row.question.issues.length || !Object.hasOwn(request.answers, row.question.id)) return false;
      if (row.question.kind === 'text') return row.controls.length === 1 && row.controls[0].value === answer;
      const selected = row.examRadios ? row.examRadios.map(vm => vm.model === vm.label) : row.controls.map(el => el.checked);
      const choices = Array.isArray(answer) ? answer : [answer];
      return choices.length > 0 && choices.every(id => row.question.options.some(option => option.id === id)) &&
        row.controls.every((el, i) => !el.indeterminate && selected[i] === choices.includes(row.question.options[i].id));
    }).map(row => row.question.id);
    return {filled};
  }

  const errors = {};
  const filled = [];
  const pending = {};
  const plans = [];
  const valuesOf = (row, property) => property === 'exam-radio'
    ? row.examRadios.map(vm => vm.model === vm.label)
    : row.controls.map(el => el[property]);
  const plan = (row, property, values) => {
    const id = row.question.id;
    const original = valuesOf(row, property);
    if (row.controls.some(el => el.indeterminate) ||
        (original.some(Boolean) && original.some((value, index) => value !== values[index]))) {
      errors[id] = '已保留页面上的现有答案。';
    } else {
      plans.push({row, property, values, original, id});
    }
  };
  for (const row of current.rows) {
    const {id, kind, issues, options} = row.question;
    if (!Object.hasOwn(request.answers, id)) continue;
    const answer = request.answers[id];
    if (issues.length || row.controls.some(el => !available(el))) {
      errors[id] = '答题控件不可编辑或题目需人工核对。';
      continue;
    }
    if (kind === 'text') {
      const el = row.controls[0];
      if (typeof answer !== 'string' || !answer.trim() || answer.length > 1000 || /[\r\u0000]/u.test(answer) ||
          (el.tagName === 'INPUT' && answer.includes('\n')) || (el.maxLength >= 0 && answer.length > el.maxLength)) {
        errors[id] = '短文本答案格式或长度不符合控件要求。';
        continue;
      }
      plan(row, 'value', [answer]);
    } else if (kind === 'single' || kind === 'multiple') {
      const choices = kind === 'single' && typeof answer === 'string' ? [answer] : answer;
      if (!Array.isArray(choices) || !choices.length || (kind === 'single' && choices.length !== 1) ||
          new Set(choices).size !== choices.length || !choices.every(value => options.some(option => option.id === value))) {
        errors[id] = '选项编号无效。';
        continue;
      }
      plan(row, row.examRadios ? 'exam-radio' : 'checked', options.map(option => choices.includes(option.id)));
    }
  }
  for (const {row, property, values, original, id} of plans) {
    if (Date.now() > request.deadline) {
      errors[id] = '填写请求已超时，请重新确认。';
      continue;
    }
    const {controls, root, question, stemNode, adapted} = row;
    const liveControls = answerControls(root);
    const liveStem = adapted ? question.stem : stemText(stemNode);
    const live = classify(root, liveStem, stemNode, liveControls, id, adapted);
    if (controls.some(el => !available(el)) || liveControls.length !== controls.length ||
        liveControls.some((el, index) => el !== controls[index]) ||
        (stemNode && (!stemNode.isConnected || !root.contains(stemNode) || !visible(stemNode))) ||
        (adapted && groupContext(root, 320) !== request.proposals[Number(id) - 1].context) ||
        (property === 'exam-radio' && (!live.examRadios || live.examRadios.some((vm, i) => vm !== row.examRadios[i]))) ||
        JSON.stringify(live.question) !== JSON.stringify(question)) {
      errors[id] = '填写期间答题控件已变化。';
      continue;
    }
    const currentValues = valuesOf(live, property);
    if (currentValues.every((value, index) => value === values[index])) {
      filled.push(id);
      continue;
    }
    if (controls.some(el => el.indeterminate) || currentValues.some((value, index) => value !== original[index])) {
      errors[id] = '已保留页面上的现有答案。';
      continue;
    }
    if (property === 'value' && controls[0].maxLength >= 0 && values[0].length > controls[0].maxLength) {
      errors[id] = '短文本答案长度不符合控件要求。';
      continue;
    }
    try {
      if (property === 'exam-radio') {
        // The exam reuses native radio names across questions. Its Vue change
        // handler selects the question-local model and saves that answer.
        const index = values.indexOf(true);
        const el = controls[index];
        const Event = el.ownerDocument.defaultView.Event;
        el.dispatchEvent(new Event('change', {bubbles: true, composed: true}));
        pending[id] = question.options[index].id;
        continue;
      }
      const changed = [];
      for (const [index, el] of controls.entries()) {
        if (currentValues[index] === values[index]) continue;
        const view = el.ownerDocument.defaultView;
        const proto = el.tagName === 'TEXTAREA' ? view.HTMLTextAreaElement.prototype : view.HTMLInputElement.prototype;
        Object.getOwnPropertyDescriptor(proto, property).set.call(el, values[index]);
        changed.push(el);
      }
      for (const el of changed) {
        if (!el.isConnected) break;
        const Event = el.ownerDocument.defaultView.Event;
        el.dispatchEvent(new Event('input', {bubbles: true, composed: true}));
        if (el.isConnected) el.dispatchEvent(new Event('change', {bubbles: true}));
      }
      if (controls.every((el, index) => el.isConnected && el[property] === values[index])) filled.push(id);
      else errors[id] = '页面未保留全部答案，请人工核对。';
    } catch {
      errors[id] = '答题控件未确认写入，请人工核对。';
    }
  }
  return Object.keys(pending).length ? {filled, errors, pending} : {filled, errors};
})()
"""
_UNREADABLE = re.compile(r'[\ue000-\uf8ff\ufffd\U000f0000-\U0010fffd]|(?:锟斤拷|烫烫烫)')


def _script(origin, registry, mode, **options):
    request = json.dumps(dict(origin=origin, registry=registry, mode=mode, exam=origin == EXAM_ORIGIN, **options))
    return f'(() => {{ const request = {request}; return ({_DOM_SCRIPT}); }})()'


def question_script(origin, registry='_rain_assessment_v1', proposals=None):
    return _script(origin, registry, 'capture', proposals=proposals)


def surface_script(origin, registry):
    return _script(origin, registry, 'surface')


def fill_script(origin, registry, proposals, expected, answers):
    deadline = int(time.time() * 1000) + 6500
    return _script(origin, registry, 'fill', proposals=proposals, expected=expected, answers=answers, deadline=deadline)


def verify_fill_script(origin, registry, proposals, expected, answers, *, complete=False):
    return _script(origin, registry, 'verify', proposals=proposals, expected=expected, answers=answers, complete=complete)


def adapt_groups(surface, groups):
    """Turn a proposed grouping into an allowlisted DOM request."""
    if not isinstance(groups, list) or not 1 <= len(groups) <= 30:
        raise ValueError('题目分组数量无效。')
    observed = {item['id']: item for item in surface['controls']}
    proposals = []
    used = set()
    for item in groups:
        if not isinstance(item, dict) or item.keys() != {'group', 'controls', 'stem'}:
            raise ValueError('题目分组格式无效。')
        group, controls, stem = item['group'], item['controls'], item['stem']
        if (not isinstance(group, str) or not isinstance(controls, list) or not 1 <= len(controls) <= 20
                or not all(isinstance(cid, str) and cid in observed and cid not in used for cid in controls)
                or len(set(controls)) != len(controls) or not isinstance(stem, str) or not stem.strip()):
            raise ValueError('题目分组引用了无效或重复的控件。')
        context = [next((entry['text'] for entry in observed[cid]['groups'] if entry['id'] == group), '')
                   for cid in controls]
        if not context[0] or any(value != context[0] for value in context) or stem not in context[0]:
            raise ValueError('题干或分组与页面观测不一致。')
        proposals.append(dict(group=group, controls=list(controls), stem=stem,
                              context=context[0], labels=[observed[cid]['label'] for cid in controls]))
        used.update(controls)
    return proposals


def normalize_capture(result):
    """Flag incomplete content instead of asking the model to reconstruct it."""
    if not isinstance(result, dict):
        raise ValueError('题页没有返回可读取的内容，请重试。')
    if result.get('error'):
        raise ValueError('请在当前雨课堂站点中打开并等待题页加载。')
    if not result.get('questions'):
        raise ValueError('没有识别到题目。请打开实际题页，特殊排版需要验证后适配。')
    questions = []
    for question in result['questions']:
        stem, options = question['stem'], question['options']
        issues = list(question['issues'])
        if not stem:
            issues.append('题干缺失')
        if len(stem) > 4000 or any(len(option['text']) > 1000 for option in options):
            issues.append('题干或选项超出读取范围')
        material = question.get('material', '')
        if len(material) > 12000:
            issues.append('共享阅读材料超出读取范围')
        if any(_UNREADABLE.search(value) for value in [stem, material, *(option['text'] for option in options)]):
            issues.append('发现乱码或专用字体编码')
        if question['kind'] in ('single', 'multiple') and len(options) < 2:
            issues.append('可读选项不足')
        questions.append({**question, 'issues': list(dict.fromkeys(issues))})
    warnings = []
    if result.get('truncated'):
        warnings.append('只读取当前页面前 80 道可见题。')
    if result.get('unreadableFrames'):
        warnings.append('存在无法读取的内嵌页面，识别结果可能不完整。')
    return questions, warnings
