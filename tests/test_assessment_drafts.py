import asyncio
from contextlib import nullcontext
from threading import RLock
from types import SimpleNamespace
from playwright.async_api import async_playwright

from browser.assessment_page import question_script, fill_script, verify_fill_script, eligibility_script
from browser.assessment_metadata import landing_details
from browser.assessment_route import EXAM_ORIGIN
from browser.edge_manager import EdgeManager
from core import assessment_model
from core.store import Store
from gui import assessment_panel


def test_landing_metadata_distinguishes_optional_attachments_and_missing_time():
    assert landing_details('截止时间：无 考试限时：不限时 添加附件（可上传1个附件）') == {
        'time_limit': '不限时', 'no_deadline': True}
    assert not landing_details('不需要上传附件，可在此输入答案')
    assert landing_details('请上传实验报告')['requires_file']
    assert landing_details('考试限时：45分钟')['time_limit'] == '45分钟'
    assert not landing_details('答题计时 00:01:08')


def test_free_response_equivalence_requires_explicit_agreement(monkeypatch):
    question = dict(id='1', kind='text', response_mode='written', stem='解释检验结论', options=[], issues=[])
    def solve(batch, key, role):
        return {'1': {'answer': role, 'reason': '统计量与结论'}}
    monkeypatch.setattr(assessment_model, '_solve_batch', solve)
    for equivalent in (True, False):
        def compare(messages, key, **kwargs):
            assert 'json' in messages[0]['content'].lower()
            return {'equivalent': equivalent}
        monkeypatch.setattr(assessment_model, '_request_json', compare)
        result, = assessment_model.review_questions([question], 'test')
        assert result['status'] == ('reviewed' if equivalent else 'withheld')


def test_agreement_multiblank_editor_and_file_only_in_edge():
    async def run():
        async with async_playwright() as engine:
            browser = await engine.chromium.launch(channel='msedge')
            try:
                page = await browser.new_page()
                await page.route(EXAM_ORIGIN + '/**', lambda route: route.fulfill(content_type='text/html', body='<main></main>'))
                await page.goto(EXAM_ORIGIN + '/start/11')
                await page.set_content("""<h1>练习册</h1><p>考试须知</p>
                    <fieldset><label><input type="checkbox" onchange="start.disabled=!this.checked">我已阅读并同意考试协议</label></fieldset>
                    <button id=start disabled onclick="document.querySelector('main').hidden=false;this.previousElementSibling.remove()">开始考试</button>
                    <main hidden><div class="subject-item"><div class="item-body"><h4 class="exam-font">均值是<input>，方差是<input>。</h4></div></div><div class="subject-item"><div class="item-body">
                    <h4 class="exam-font">请解释</h4><div class="ueditor-content"><iframe srcdoc='
                    <body contenteditable="true"><p><br></p></body>'></iframe></div>可上传附件<input type=file></div></div></main>
                    <button onclick="window.submitted=true">交卷</button>""")
                manager = EdgeManager({'base_url': 'https://www.yuketang.cn'})
                manager.page = page
                assert await manager.start_assessment({'title': '练习册'}, 'https://www.yuketang.cn/course', lambda: True)
                assert await page.locator('main').is_visible()
                registry = '_draft_test'
                snap = await page.evaluate(question_script(EXAM_ORIGIN, registry))
                assert [q['kind'] for q in snap['questions']] == ['text']*3
                assert '均值是 【空1】 ，方差是 【空2】' in snap['questions'][0]['stem']
                assert '第 2 / 2 空' in snap['questions'][1]['stem']
                answers = {'1': '12', '2': '6', '3': '第一行\n第二行'}
                result = await page.evaluate(fill_script(EXAM_ORIGIN, registry, None, snap, answers))
                assert not result['errors']
                assert (await page.evaluate(verify_fill_script(EXAM_ORIGIN, registry, None, snap, answers)))['filled'] == list(answers)
                assert not await page.evaluate('Boolean(window.submitted)')
                await page.set_content('<fieldset><legend>请上传实验报告</legend><input type=file></fieldset>')
                assert (await page.evaluate(eligibility_script(EXAM_ORIGIN, registry)))['requires_file']
                assert 'error' in await page.evaluate(question_script(EXAM_ORIGIN, registry))
            finally:
                await browser.close()
    asyncio.run(run())

def test_word_table_placeholders_are_scoped_to_readable_headers():
    async def run():
        async with async_playwright() as engine:
            browser = await engine.chromium.launch(channel="msedge")
            try:
                page = await browser.new_page()
                await page.route(EXAM_ORIGIN + "/**", lambda route: route.fulfill(
                    content_type="text/html", body="<main></main>"))
                await page.goto(EXAM_ORIGIN + "/exam/11")
                spacer = '<img src="/fe/ueditor/themes/default/images/spacer.gif" word_img="file:///example.gif">'
                await page.set_content(f'<section data-question-id="1"><div data-question-stem>检验比例'
                    f'<table><tr><th>理论次数{spacer}</th><th>观测次数</th></tr>'
                    '<tr><td>20</td><td>13</td></tr></table></div><textarea></textarea></section>')
                script = question_script(EXAM_ORIGIN)
                capture = await page.evaluate(script)
                q, = capture['questions']
                assert not q['issues'] and '20 | 13' in q['stem'] and '〔原符号未显示〕' in q['stem']
                assert 'file:' not in q['stem']
                assert (await page.evaluate(fill_script(EXAM_ORIGIN, '_rain_assessment_v1', None,
                                                         capture, {'1': '草稿'})))['filled'] == ['1']
                await page.locator('th img').evaluate("img => img.src='/actual-chart.png'")
                assert (await page.evaluate(script))['questions'][0]['issues']
                await page.locator('table').evaluate("(table, img) => table.rows[1].cells[1].innerHTML=img", spacer)
                await page.locator('th img').evaluate("img => img.remove()")
                assert (await page.evaluate(script))['questions'][0]['issues']
            finally:
                await browser.close()
    asyncio.run(run())


def test_selected_assignment_dispatches_deepseek_once_after_verified_navigation(tmp_path, monkeypatch):
    store = Store(tmp_path)
    store.set('account', {'id': 'student'})
    task = dict(id='11:22', title='配对数据检验', kind='exam', state='manual')
    inventory = dict(account='student', phase='done', tasks=[task])
    store.set('assessment_inventory', inventory)
    page_tools = SimpleNamespace(navigation_revision=0, requires_file=lambda: False)
    browser = SimpleNamespace(interactive=True, rect=(0, 0, 500, 400),
                              snapshot=lambda: {'phase': 'ready'}, document_digest=lambda: 'verified-page',
                              agent_session=lambda cancelled: nullcontext(page_tools))
    owner = SimpleNamespace(store=store, embedded_browser=browser, demo=False, watchdog=True,
                            _busy=lambda: False, _alive=lambda: True, _lock=RLock(), closing=False)
    panel = assessment_panel.AssessmentPanel(owner)
    panel.key = 'test'
    calls = []

    def record_run(key, messages, **kwargs):
        names = [t['name'] for t in kwargs['tools']]
        assert {'read_questions', 'fill_reviewed_answers'} <= set(names)
        assert not any('submit' in name for name in names)
        assert kwargs['model'] == 'deepseek-v4-pro'
        calls.append(kwargs['model'])
        return {'text': ''}

    monkeypatch.setattr(assessment_panel, 'run_agent', record_run)
    monkeypatch.setattr(panel, '_start', lambda operation, work, done: done(work()))
    panel.action('assessment_select_task', dict(id='11:22', model='deepseek-v4-pro', effort='high', web_search=False))
    entry = store.get('assessment_entry')
    panel.snapshot(inventory, 'student', 'assessment_open', True, entry)
    assert calls == []
    entry.update(phase='opened', document='verified-page')
    for _ in range(2):
        panel.snapshot(inventory, 'student', 'idle', True, entry)
    assert calls == ['deepseek-v4-pro']
    assert '实际写入 0 题' in panel.history[-1]['text']
