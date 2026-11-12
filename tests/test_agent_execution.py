"""Offline execution proof: no credentials, provider calls or real assignments."""
import asyncio
import json

from playwright.async_api import async_playwright

from browser.assessment_page import question_script, fill_script, verify_fill_script
from browser.assessment_submit import submission_script
from core import assessment_agent
from gui.assessment_tools import BrowserAgentTools
from gui import assessment_tools


def test_model_must_try_tools_before_finishing_an_execution(monkeypatch):
    calls = []
    def response(_key, payload):
        calls.append(list(payload['input']))
        if len(calls) == 1:
            return [{'type': 'message', 'content': [{'type': 'output_text', 'text': '答案是 2。'}]}]
        if len(calls) == 2:
            return [{'type': 'function_call', 'call_id': 'read-1', 'name': 'read_questions',
                     'arguments': '{"start":0}'}]
        return [{'type': 'message', 'content': [{'type': 'output_text', 'text': '完成读取。'}]}]
    monkeypatch.setattr(assessment_agent, '_request_response', response)
    used = []
    result = assessment_agent.run_agent('fake-key', [{'role':'user', 'content':'执行模拟任务'}],
        model='deepseek-flash', effort='high', web_search=False,
        tools=[{'type':'function', 'name':'read_questions'}],
        execute=lambda name, args: used.append((name, args)) or {'questions':[]},
        cancelled=lambda:False, completion_hint=lambda: '请实际读取题页。' if not used else None)
    assert used == [('read_questions', {'start': 0})]
    assert len(calls) == 3 and result['text'] == '完成读取。'


def test_finish_submits_once_only_after_all_reviewed_answers(monkeypatch):
    questions = [dict(id='1', kind='single', stem='1+1?', issues=[], options=[dict(id='1', text='2')])]
    class Browser:
        navigation_revision = 1
        submissions = 0
        def read_assessment_page(self): return questions, [], 1
        def revalidate_assessment_page(self, _capture): pass
        def fill_assessment_answers(self, _capture, answers, **kwargs):
            return {'filled': list(answers), 'errors': {}}
        def submit_assessment_answers(self, _capture, _answers, **kwargs):
            self.submissions += 1
            self.navigation_revision += 1  # The real site navigates to /cover/id.
            return {'state':'submitted', 'message':'提交成功'}
    monkeypatch.setattr(assessment_tools, 'review_questions', lambda *a, **kw: [
        {'id':'1', 'status':'reviewed', 'answer':'1'}])
    browser = Browser()
    agent = BrowserAgentTools(browser, 'fake', fill=True, cancelled=lambda:False, on_event=lambda _:None)
    assert agent.execute('submit_checked_answers', {}).get('error')
    assert not browser.submissions
    agent.execute('read_questions', {'start':0})
    agent.execute('fill_reviewed_answers', {'answers':[{'id':'1', 'answer':'1'}]})
    assert agent.finish()['submission']['state'] == 'submitted'
    assert not agent.stopped()
    assert agent.finish()['submission']['state'] == 'submitted'
    assert browser.submissions == 1
    assert agent.execute('browser_click', {'ref':'again'})['state'] == 'submitted'
    readonly = BrowserAgentTools(browser, 'fake', fill=False, cancelled=lambda:False, on_event=lambda _:None)
    assert 'submit_checked_answers' not in {t['name'] for t in readonly.specs()}
    assert readonly.execute('submit_checked_answers', {}).get('error')


def test_disputes_are_not_resampled_and_defer_blocks_finish(monkeypatch):
    questions = [dict(id='1', kind='single', stem='Question', issues=[],
                      options=[dict(id='1', text='Choice')])]
    reviews = []
    class Browser:
        navigation_revision = 1
        def read_assessment_page(self): return questions, [], 1
        def revalidate_assessment_page(self, _capture): pass
        def submit_assessment_answers(self, *args, **kwargs):
            raise AssertionError('A disputed or deferred task must never submit')
    def review(rows, *args, **kwargs):
        reviews.append(rows)
        return [{'id':'1', 'status':'withheld', 'answer':None, 'reason':'Disagreement'}]
    monkeypatch.setattr(assessment_tools, 'review_questions', review)
    agent = BrowserAgentTools(Browser(), 'fake', fill=True, cancelled=lambda:False, on_event=lambda _:None)
    agent.execute('read_questions', {'start':0})
    for _ in range(3):
        assert agent.execute('fill_reviewed_answers', {'answers':[{'id':'1','answer':'1'}]})['withheld'] == ['1']
    assert len(reviews) == 1
    assert agent.finish()['submission']['state'] == 'blocked'
    agent.execute('defer_task', {'reason':'Unresolved ambiguity'})
    assert agent.finish()['submission']['message'] == 'Unresolved ambiguity'


def test_adjudication_requires_source_evidence_and_valid_answer(monkeypatch):
    from core import assessment_model
    import pytest
    question = dict(id='1', kind='single', stem='Question', issues=[],
                    material='The evidence comes directly from this reading passage.',
                    options=[dict(id='1', text='Choice')])
    calls = []
    def response(*args, **kwargs):
        calls.append(kwargs)
        return {'answer':'1', 'confidence':'high', 'reason':'Supported by the passage'}
    monkeypatch.setattr(assessment_model, '_request_json', response)
    with pytest.raises(assessment_model.AssessmentModelError):
        assessment_model.resolve_conflict(question, '1', 'This quote does not exist in the passage.', 'fake')
    assert not calls
    result = assessment_model.resolve_conflict(question, '1', 'The evidence comes directly from this reading passage.', 'fake')
    assert result['status'] == 'reviewed'
    assert calls[0]['model'] == 'deepseek-v4-pro'


def test_submission_checks_actual_values_dialog_and_receipt_in_edge():
    async def scenario():
        async with async_playwright() as engine:
            browser = await engine.chromium.launch(channel='msedge', headless=True)
            page = await browser.new_page()
            sent = []
            html = '''<meta charset="utf-8"><form onsubmit="event.preventDefault(); window.clicks++; document.querySelector('dialog').showModal()">
              <fieldset><legend>1+1?</legend><label><input type=radio name=q>2</label><label><input type=radio name=q>3</label></fieldset>
              <fieldset><legend>选择偶数</legend><label><input type=checkbox>2</label><label><input type=checkbox>4</label></fieldset>
              <fieldset><legend>填写数字</legend><textarea></textarea></fieldset>
              <button type=submit>交卷</button></form>
              <dialog><p>确认交卷？</p><button onclick="confirmSubmit()">确定</button></dialog>
              <script>window.clicks=0;window.confirms=0;
              async function confirmSubmit(){window.confirms++;document.querySelector('dialog').close();
                const r=await fetch('/save',{method:'POST'});if(r.ok){
                  const s=document.createElement('div');s.setAttribute('role','status');s.textContent='提交成功';document.body.append(s)}}
              </script>'''
            async def route(request):
                if request.request.method == 'POST':
                    sent.append(request.request.url)
                    await request.fulfill(status=200, body='{}')
                else:
                    await request.fulfill(content_type='text/html', body=html)
            await page.route('https://study.test/**', route)
            try:
                await page.goto('https://study.test/fixture')
                expected = await page.evaluate(question_script('https://study.test'))
                answers = {'1':'1','2':['1','2'],'3':'2'}
                def script(begin=False):
                    return submission_script('https://study.test', '_rain_assessment_v1', None, expected, answers, begin=begin)
                assert (await page.evaluate(script(True)))['state'] == 'blocked'
                assert not sent
                result = await page.evaluate(fill_script('https://study.test', '_rain_assessment_v1', None, expected, answers))
                assert result['filled'] == ['1','2','3']
                assert (await page.evaluate(verify_fill_script('https://study.test', '_rain_assessment_v1', None, expected, answers)))['filled'] == ['1','2','3']
                # A user edit after review must stop submission.
                await page.locator('textarea').fill('wrong')
                assert (await page.evaluate(script(True)))['state'] == 'blocked'
                await page.locator('textarea').fill('2')
                submitted = await page.evaluate(script(True))
                assert submitted['state'] == 'waiting', submitted
                assert not sent and await page.evaluate('clicks') == 1
                assert (await page.evaluate(script()))['state'] == 'waiting'
                await page.get_by_role('status').wait_for()
                assert (await page.evaluate(script()))['state'] == 'submitted'
                assert (await page.evaluate(script(True)))['state'] == 'submitted'
                assert len(sent) == 1 and await page.evaluate('[clicks,confirms]') == [1,1]
                # Missing server receipt never becomes a claimed success or a retry.
                await page.reload()
                expected = await page.evaluate(question_script('https://study.test'))
                await page.evaluate(fill_script('https://study.test', '_rain_assessment_v1', None, expected, answers))
                await page.evaluate('() => {window.confirmSubmit=()=>{window.confirms++;document.querySelector("dialog").close()}}')
                await page.evaluate(script(True))
                await page.evaluate(script())
                for _ in range(3):
                    assert (await page.evaluate(script(True)))['state'] == 'waiting'
                assert await page.evaluate('[clicks,confirms]') == [1,1]
                # Hidden/paginated questions prevent an incomplete submission.
                await page.reload()
                expected = await page.evaluate(question_script('https://study.test'))
                await page.evaluate(fill_script('https://study.test', '_rain_assessment_v1', None, expected, answers))
                await page.evaluate('document.body.insertAdjacentHTML("beforeend","<button>下一页</button>")')
                assert (await page.evaluate(script(True)))['state'] == 'blocked'
                assert await page.evaluate('clicks') == 0
                await page.get_by_role('button', name='下一页').evaluate('el=>el.remove()')
                await page.evaluate('document.body.insertAdjacentHTML("beforeend","<div>遗漏的自定义题目<textarea></textarea></div>")')
                omitted = await page.evaluate(script(True))
                assert omitted['state'] == 'blocked' and '未纳入' in omitted['message']
                assert await page.evaluate('clicks') == 0
            finally:
                await browser.close()
    asyncio.run(scenario())
