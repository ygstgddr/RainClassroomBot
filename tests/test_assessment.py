import asyncio
import json

from playwright.async_api import async_playwright

from browser.assessment_page import fill_script, normalize_capture, question_script, verify_fill_script
from browser.assessment_route import EXAM_ORIGIN, assessment_origin
from browser.edge_manager import EdgeManager
from browser.site_adapter import SiteAdapter
from core import assessment_model
from core.store import Store
from gui import assessment_tools
from task.assessment_scan import open_assessment, scan_assessments
from tests.test_tools_frontend import display_snapshot, open_page


def test_inventory_uses_all_courses_without_changing_learning_selection(tmp_path):
    class Catalog(SiteAdapter):
        async def account(self):
            self.account_id = 'account'
            return {'id': 'account'}

        async def courses(self):
            return [{'classroom_id': str(cid), 'name': f'课程 {cid}'} for cid in (1, 2)]

        async def context(self, cid):
            return {'cid': cid}

        async def get(self, path, params=None, headers=None):
            assert path == '/mooc-api/v1/lms/learn/course/chapter'
            leaves = [{'id': i, 'leaf_type': i, 'name': f'任务 {i}'} for i in (0, 5, 6, 9)]
            leaves[-1]['is_locked'] = True
            return {'course_chapter': [{'section_leaf_list': [{'leaf_list': leaves}]}]}

        async def schedule(self, cid):
            return {'5': {'status': 3}, '6': None}

    store = Store(tmp_path)
    store.select_courses(['1'], 'account')
    store.set('desired', 'assessment_scan')
    store.set('assessment_inventory', {'id': 'run', 'account': 'account', 'phase': 'queued'})
    asyncio.run(scan_assessments(Catalog(None, {}), store, 2))
    report = store.get('assessment_inventory')
    assert report['phase'] == 'done' and report['finished'] == 2
    assert {(task['kind'], task['state']) for task in report['tasks']} == {
        ('exam', 'completed'), ('homework', 'unknown'), ('quiz', 'locked')}
    assert len(report['tasks']) == 6
    assert store.selected_courses('account') == ['1'] and store.rows('account') == []


def test_review_validates_ids_independent_choices_and_skips_unreadable(monkeypatch):
    options = [{'id': str(i), 'text': f'{letter}. 样例选项'} for i, letter in enumerate('ABC', start=1)]
    questions = [
        dict(id=str(i), kind=kind, stem='公开样例', options=options, issues=[])
        for i, kind in [(1, 'multiple'), (2, 'single'), (3, 'single')]
    ]
    questions.append(dict(id='4', kind='', stem='不应发送的乱码题', issues=['缺字']))
    requests = []

    def reply(messages, key, *, reason=False):
        assert key == 'test-key' and reason
        expected = json.loads(messages[1]['content'])['questions']
        assert [question['id'] for question in expected] == ['1', '2', '3']
        assert '不应发送的乱码题' not in messages[1]['content']
        requests.append(messages[0]['content'])
        values = [['1', '3'], '1', None] if len(requests) == 1 else [['C', 1], '2', 'unknown']
        return {'results': [dict(id=str(i), answer=value, reason='依据')
                            for i, value in enumerate(values, start=1)]}

    monkeypatch.setattr(assessment_model, '_request_json', reply)
    results = assessment_model.review_questions(questions, 'test-key')
    assert len(set(requests)) == 2
    assert [row['status'] for row in results] == ['reviewed', 'withheld', 'withheld', 'skipped']
    assert results[0]['answer'] == ['1', '3']
    assert all(row['answer'] is None for row in results[1:])
    assert '不一致' in results[1]['reason'] and '未能确定' in results[2]['reason']
    assert assessment_model.canonical_answer(questions[0], ['A', '1']) is None
    assert assessment_model.canonical_answer(questions[1], 'D') is None


def test_approved_entry_is_consumed_and_never_retried_after_interruption(tmp_path):
    class Adapter:
        async def account(self):
            return {'id': 'student'}

        async def scan_course(self, course, *, assessments_only):
            assert assessments_only and course['classroom_id'] == '1'
            return [{'leaf_id': '7', 'kind': 'homework', 'state': 'manual'}]

    class Browser:
        calls = 0
        starts = 0
        page = type('Page', (), {'url': 'https://study.test/course'})()

        async def open_task(self, task, *, entry_allowed):
            self.calls += 1
            assert task['leaf_id'] == '7' and entry_allowed()
            assert store.get('assessment_entry')['phase'] == 'opening'
            self.page.url = 'https://study.test/example'
            return self.page

        async def start_assessment(self, task, course_url, allowed):
            assert course_url.endswith('/course') and allowed()
            self.starts += 1
            return True

    store, browser = Store(tmp_path), Browser()
    request = {'id': 'approved', 'account': 'student', 'task_id': '1:7', 'resume': 'paused'}
    store.set('desired', 'assessment_open')
    store.set('assessment_entry', {**request, 'phase': 'queued'})
    assert asyncio.run(open_assessment(Adapter(), browser, store)) == 'paused'
    assert browser.calls == browser.starts == 0
    request['start_authorized'] = True
    store.set('assessment_entry', {**request, 'phase': 'opening'})
    assert asyncio.run(open_assessment(Adapter(), browser, store)) == 'paused'
    assert browser.calls == 0
    store.set('assessment_entry', {**request, 'phase': 'queued'})
    assert asyncio.run(open_assessment(Adapter(), browser, store)) == 'paused'
    assert store.get('assessment_entry')['phase'] == 'opened' and browser.calls == browser.starts == 1
    asyncio.run(open_assessment(Adapter(), browser, store))
    assert browser.calls == 1


def test_exam_host_start_and_shared_passage_with_native_options():
    site = 'https://www.yuketang.cn'
    assert assessment_origin(EXAM_ORIGIN + '/exam/1', site) == EXAM_ORIGIN
    for unrelated in ('https://examination.xuetangx.com.evil.test/exam/1', EXAM_ORIGIN + '/login',
                      'http://examination.xuetangx.com/exam/1'):
        assert assessment_origin(unrelated, site) is None

    async def scenario():
        landing = '<div class="header-title">模拟阅读测验</div><form action="/exam/2"><button>开始作答</button></form>'
        exam = '''<div class="header-title">模拟阅读测验</div>
        <div class="dict_list"><div><div class="dict_description">共享的本地样例阅读材料</div>
        <section class="subject-item"><div class="item-body"><h4 class="exam-font">题目一</h4>
        <label class="el-radio" role="radio"><input type="radio" name="radio0">甲</label>
        <label class="el-radio" role="radio"><input type="radio" name="radio1">乙</label></div></section>
        <section class="subject-item"><div class="item-body"><h4 class="exam-font">题目二</h4>
        <label class="el-radio" role="radio"><input type="radio" name="radio0">丙</label>
        <label class="el-radio" role="radio"><input type="radio" name="radio1">丁</label></div></section></div></div>
        <script>
          for (const root of document.querySelectorAll('.subject-item')) {
            const parent = {$el:root.querySelector('.item-body'), value:null};
            [...root.querySelectorAll('label')].forEach((label, index) => {
              label.__vue__ = {$options:{name:'ElRadio'}, $el:label, $parent:parent, label:index,
                get model() { return parent.value; }};
              label.querySelector('input').addEventListener('change', () => { parent.value = index; });
            });
          }
        </script>'''
        async with async_playwright() as engine:
            edge = await engine.chromium.launch(channel='msedge', headless=True)
            try:
                manager = EdgeManager({'base_url': site})
                manager.page = page = await edge.new_page()
                await page.route(EXAM_ORIGIN + '/**', lambda route: route.fulfill(
                    body=landing if '/exam/1' in route.request.url else exam, content_type='text/html; charset=utf-8'))
                await page.goto(EXAM_ORIGIN + '/exam/1')
                assert await manager.start_assessment({'title': '模拟阅读测验'}, site + '/course', lambda: True)
                captured = await page.evaluate(question_script(EXAM_ORIGIN, 'registry'))
                questions, warnings = normalize_capture(captured)
                assert not warnings and len(questions) == 2
                assert questions[0]['material'] == '共享的本地样例阅读材料'
                assert questions[0]['stem'] == '题目一' and questions[0]['issues'] == []
                assert [option['text'] for option in questions[0]['options']] == ['甲', '乙']
                assert assessment_model._validate_questions(questions)[0]['material'] == questions[0]['material']
                answers = {'1': '2', '2': '2'}
                result = await page.evaluate(fill_script(EXAM_ORIGIN, 'registry', None, captured, answers))
                assert result == {'filled': [], 'errors': {}, 'pending': answers}
                assert await page.evaluate(verify_fill_script(EXAM_ORIGIN, 'registry', None, captured, answers)) == {
                    'filled': ['1', '2']}
            finally:
                await edge.close()

    asyncio.run(scenario())


def test_agent_fill_tool_requires_approval_and_reports_partial_review(monkeypatch):
    options = [dict(id='1', text='A. 甲'), dict(id='2', text='B. 乙')]
    questions = [dict(id=str(i), kind='single', stem='题目', issues=[], options=options) for i in range(1, 4)]
    questions.append(dict(id='4', issues=['缺图']))

    class Browser:
        navigation_revision = 1
        fills = []

        def read_assessment_page(self):
            return questions, [], 1

        def revalidate_assessment_page(self, capture):
            assert capture[2] == self.navigation_revision

        def fill_assessment_answers(self, capture, answers, *, cancelled):
            assert not cancelled()
            self.fills.append(answers)
            return {'filled': list(answers), 'errors': {}}

    monkeypatch.setattr(assessment_tools, 'review_questions', lambda *_args, **_kwargs: [
        {'id': '1', 'status': 'reviewed', 'answer': '1'},
        {'id': '2', 'status': 'withheld', 'answer': None, 'reason': '两轮独立复核的答案不一致。'},
        {'id': '3', 'status': 'reviewed', 'answer': '2'}])
    browser = Browser()
    read = assessment_tools.BrowserAgentTools(browser, 'fixture', fill=False, cancelled=lambda: False, on_event=lambda _: None)
    assert 'fill_reviewed_answers' not in {tool['name'] for tool in read.specs()}
    receipts = []
    agent = assessment_tools.BrowserAgentTools(browser, 'fixture', fill=True, cancelled=lambda: False,
                                               on_event=lambda _: None, on_receipt=receipts.append)
    assert agent.execute('read_questions', {'start': 0})['questions'] == questions
    proposals = {'answers': [dict(id=str(i), answer=value) for i, value in enumerate(['A', '2', '1', '1'], 1)]}
    result = agent.execute('fill_reviewed_answers', proposals)
    assert result['filled'] == ['1', '3'] and result['corrected'] == ['3']
    assert set(result['withheld']) == {'2', '4'}
    assert '不一致' in result['review_notes']['2'] and '缺图' in result['review_notes']['4']
    assert receipts == [result]
    retry = agent.execute('fill_reviewed_answers', proposals)
    assert set(retry['withheld']) == {'2', '4'}
    assert retry['filled'] == ['1', '3']
    assert len(browser.fills) == 2
    agent.execute('fill_reviewed_answers', proposals)
    assert 'error' in agent.execute('fill_reviewed_answers', proposals)


def test_reader_and_answer_panel_in_edge():
    async def scenario():
        async with async_playwright() as engine:
            browser = await engine.chromium.launch(channel='msedge', headless=True)
            try:
                page = await browser.new_page()
                await page.route('https://study.test/**', lambda route: route.fulfill(
                    content_type='text/html', body='''
                    <form><fieldset><legend>1 + 1 = ?</legend>
                    <label><input type="radio" name="sum">2</label>
                    <label><input type="radio" name="sum">3</label></fieldset>
                    <fieldset><legend>缺字 &#57344;</legend><textarea>已有答案</textarea></fieldset>
                    <button type="submit">交卷</button></form>
                    '''))
                await page.goto('https://study.test/fixture')
                result = await page.evaluate(question_script('https://study.test'))
                questions, warnings = normalize_capture(result)
                assert not warnings and len(questions) == 2
                assert questions[0]['stem'] == '1 + 1 = ?'
                assert [item['text'] for item in questions[0]['options']] == ['2', '3']
                assert questions[1]['issues'] and '已有答案' not in json.dumps(result, ensure_ascii=False)
                assert not await page.locator('input[type=radio]:checked').count()

                snapshot, actions = display_snapshot(), []
                snapshot['browser'] = dict(enabled=True, phase='ready', can_navigate=True)
                snapshot['development'] = True
                snapshot['assessment'] = dict(available=False, has_key=False, phase='idle')
                panel = await open_page(browser, snapshot, actions)
                errors = []
                panel.on('pageerror', lambda error: errors.append(str(error)))
                await panel.locator('[data-page=browser]').click()
                assert await panel.locator('.browser-heading #browser-assessment-tools').inner_text() == '自动答题(实验性)'
                await panel.locator('#browser-assessment-tools').click()
                await panel.wait_for_function('!RainUI.getState().pending')
                assert await panel.locator('#assessment-reconnect').is_visible()
                assert await panel.locator('#assessment-scan').is_disabled()
                assert '开发版' in await panel.locator('#version-label').inner_text()
                snapshot['assessment']['available'] = True
                await panel.evaluate('s => RainUI.applySnapshot(s)', snapshot)
                assert await panel.locator('#assessment-panel').is_visible()
                assert await panel.locator('#assessment-reconnect').is_hidden()
                await panel.locator('#assessment-key').fill('fixture-key')
                await panel.locator('#assessment-save-key').click()
                await panel.wait_for_function('!RainUI.getState().pending')
                assert ('assessment_save_key', {'api_key': 'fixture-key'}) in actions
                assert await panel.locator('#assessment-key').input_value() == ''
                snapshot['assessment']['has_key'] = True
                await panel.evaluate('s => RainUI.applySnapshot(s)', snapshot)
                assert await panel.locator('#assessment-key-form').is_hidden()
                await panel.locator('#assessment-effort-toggle').click()
                await panel.locator('#assessment-effort').press('End')
                assert await panel.locator('#assessment-effort').get_attribute('aria-valuetext') == '最大'
                await panel.locator('#assessment-effort').press('ArrowLeft')
                await panel.locator('#assessment-effort').press('Escape')
                assert await panel.locator('#assessment-effort-popover').is_hidden()
                assert await panel.locator('#assessment-effort-toggle').evaluate('el => el === document.activeElement')
                assert await panel.locator('#assessment-web').is_disabled()
                await panel.locator('#assessment-model-toggle').click()
                await panel.get_by_role('menuitemradio', name='Flash').press('End')
                await panel.get_by_role('menuitemradio', name='V4 Pro').press('Enter')
                assert await panel.locator('#assessment-model').input_value() == 'deepseek-v4-pro'
                assert await panel.locator('#assessment-model-menu').is_hidden()
                assert await panel.locator('#assessment-model-current').inner_text() == 'V4 Pro'
                await panel.locator('#assessment-model-toggle').press('ArrowDown')
                assert await panel.get_by_role('menuitemradio', name='V4 Pro').get_attribute('aria-checked') == 'true'
                await panel.get_by_role('menuitemradio', name='V4 Pro').press('Escape')
                assert await panel.locator('#assessment-model-toggle').evaluate('el=>el===document.activeElement')
                await panel.locator('#assessment-model-toggle').click()
                await panel.locator('#assessment-effort-toggle').click()
                assert await panel.locator('#assessment-model-menu').is_hidden()
                await panel.locator('#assessment-effort').press('Escape')
                await panel.locator('#assessment-web').check()
                await panel.locator('#assessment-chat-input').fill('离线样例问题')
                await panel.locator('#assessment-send').click()
                await panel.wait_for_function('!RainUI.getState().pending')
                assert ('assessment_chat', {'text': '离线样例问题', 'model': 'deepseek-v4-pro',
                    'effort': 'high', 'web_search': True, 'read_page': False}) in actions
                snapshot['assessment']['history'] = [
                    {'role': 'user', 'text': '## **保持原样**'},
                    {'role': 'assistant',
                     'text': '## **复习要点**\n- **定义**\n- <img src=x onerror="window.BAD_UI=true">\n\n' + '长对话样例\n' * 60,
                     'steps': ['读取题页']}]
                receipt = {'filled': ['1'], 'review_notes': {'2': '两轮独立复核的答案不一致。'},
                           'errors': {'3': '页面未确认选中。'}, 'corrected': ['1']}
                snapshot['assessment']['fill_receipt'] = receipt
                await panel.evaluate('s => RainUI.applySnapshot(s)', snapshot)
                assert await panel.locator('.assessment-user').inner_text() == '## **保持原样**'
                assert await panel.locator('.assessment-response h4 strong').inner_text() == '复习要点'
                assert await panel.locator('.assessment-response li').count() == 2
                assert '<img src=x' in await panel.locator('.assessment-response li').last.inner_text()
                assert not await panel.locator('.assessment-response img').count()
                assert await panel.locator('.assessment-response').evaluate('el=>getComputedStyle(el).userSelect') == 'text'
                await panel.evaluate('''() => {
                    Object.defineProperty(navigator, 'clipboard', {configurable:true,value:{writeText:async text=>{window.copied=text}}});
                }''')
                await panel.get_by_role('button', name='复制回复').click()
                assert await panel.evaluate('window.copied') == snapshot['assessment']['history'][-1]['text']
                await panel.evaluate('''() => {
                    navigator.clipboard.writeText=async()=>{throw new Error('unavailable')};
                    document.execCommand=name=>{window.fallbackCopied=document.activeElement.value;return name==='copy'};
                }''')
                await panel.get_by_role('button', name='复制回复').click()
                assert await panel.evaluate('window.fallbackCopied') == snapshot['assessment']['history'][-1]['text']
                summary = panel.locator('.assessment-receipt')
                assert await summary.count() == 1
                assert '已写入 1 题' in await summary.inner_text()
                assert '第 2 题 · 未通过复核' in await summary.inner_text()
                assert '第 3 题 · 未写入' in await summary.inner_text()
                snapshot['assessment']['history'][-1]['receipt'] = snapshot['assessment'].pop('fill_receipt')
                await panel.evaluate('s => RainUI.applySnapshot(s)', snapshot)
                assert await panel.locator('.assessment-message .assessment-receipt').count() == 1
                for size in ({'width': 1280, 'height': 850}, {'width': 900, 'height': 660}):
                    await panel.set_viewport_size(size)
                    await panel.wait_for_function('''() => {
                        const chat = document.querySelector('#assessment-chat');
                        const dock = document.querySelector('#assessment-dock').getBoundingClientRect();
                        const panel = document.querySelector('#assessment-panel').getBoundingClientRect();
                        const viewport = document.querySelector('#browser-viewport').getBoundingClientRect();
                        return chat.scrollHeight > chat.clientHeight &&
                            chat.getBoundingClientRect().bottom <= dock.top + 1 &&
                            dock.bottom <= panel.bottom + 1 && dock.bottom < innerHeight &&
                            viewport.right < panel.left && viewport.width >= 425 &&
                            document.documentElement.scrollWidth === innerWidth;
                    }''')
                await panel.locator('[data-action=assessment_tab_tasks]').click()
                assert await panel.locator('#assessment-empty-title').inner_text() == '还没有扫描任务'
                await panel.locator('#assessment-scan').click()
                await panel.wait_for_function('!RainUI.getState().pending')
                assert ('assessment_scan', {}) in actions
                pending = {'id': '1:7', 'title': '模拟测验', 'course': '模拟课程', 'kind': 'quiz', 'state': 'manual'}
                complete = dict(pending, id='1:8', title='已提交的作业', kind='homework', state='completed')
                snapshot['assessment'].update(phase='done', tasks=[pending, complete])
                await panel.evaluate('s => RainUI.applySnapshot(s)', snapshot)
                assert await panel.locator('#assessment-task-count').inner_text() == '1'
                assert await panel.locator('#assessment-progress').inner_text() == '1 项未完成 · 测验 1'
                assert await panel.locator('#assessment-tasks li').count() == 1
                assert not await panel.get_by_text('已提交的作业', exact=True).count()
                await panel.get_by_role('button', name='交给 Agent').click()
                await panel.wait_for_function('!RainUI.getState().pending')
                assert ('assessment_select_task', {'id': '1:7', 'model': 'deepseek-v4-pro',
                    'effort': 'high', 'web_search': True}) in actions
                assert await panel.locator('#assessment-chat-view').is_visible()
                await panel.locator('[data-action=assessment_tab_tasks]').click()
                snapshot['assessment']['tasks'] = [complete]
                await panel.evaluate('s => RainUI.applySnapshot(s)', snapshot)
                assert await panel.locator('#assessment-task-count').is_hidden()
                assert not await panel.locator('#assessment-tasks li').count()
                assert await panel.locator('#assessment-empty-title').inner_text() == '没有待办任务'
                snapshot['assessment']['failed'] = ['未读取的课程']
                await panel.evaluate('s => RainUI.applySnapshot(s)', snapshot)
                assert await panel.locator('#assessment-empty-title').inner_text() == '暂未找到待办任务'
                await panel.locator('#assessment-summary-title').click()
                assert await panel.get_by_text('未读取的课程', exact=True).is_visible()
                assert await panel.evaluate('storageWrites') == 0
                assert await panel.locator('#browser-viewport').is_visible()
                assert not errors
            finally:
                await browser.close()

    asyncio.run(scenario())
