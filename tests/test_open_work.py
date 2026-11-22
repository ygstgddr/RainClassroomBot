import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock
from contextlib import asynccontextmanager

from playwright.async_api import async_playwright

from browser import open_work


def test_open_work_uses_pro_for_answer_and_review(monkeypatch):
    calls = []
    def model(key, messages, **options):
        calls.append((options, messages))
        return {'text': '{"action":"reply","text":"氨"}'}
    monkeypatch.setattr(open_work, 'run_agent', model)
    prefs = dict(model='deepseek-flash', effort='high', web_search=False)
    reply = open_work.draft_reply('微生物学', '生物固氮', '', [{'role':'tutor','text':'产物是什么？'}],
                                  'test', prefs, lambda: False)
    assert reply == ('reply', '氨')
    assert [opts['model'] for opts, _ in calls] == ['deepseek-v4-pro'] * 2
    assert 'candidate' in calls[1][1][-1]['content']
    assert prefs['model'] == 'deepseek-flash'


def test_open_work_enters_chat_types_once_and_reads_score(monkeypatch, tmp_path):
    from core.store import Store
    async def run():
        async with async_playwright() as engine:
            browser = await engine.chromium.launch(channel='msedge')
            try:
                page = await browser.new_page()
                site = 'https://study.invalid'
                exercise = '/ai-workspace/lms-graph/11/exercise/22'
                landing = '/v2/web/iframe-exercise/11/22'
                wrapper = '/v2/web/cloud/student/exercise/openWorkAgent/11/22/33'
                inner = '/ai-workspace/open-work-agent'
                tutor = ('<div class="msg-item"><div class="user-received-item"><div class="user-received-message">'
                         '<div class="message-content"><p class="ai-text-message">请回答</p></div>'
                         '<div class="chat-received-bar"><i class="icon-fuzhi1"></i></div></div></div></div>')
                pages = {
                    exercise: f'<b class="learning-space-header__primary">统计学</b><iframe src="{landing}"></iframe>',
                    landing: f'<div class="agent-card"><div class="agent-actions"><button onclick="location.href=\'{wrapper}\'">继续作答</button></div></div>',
                    wrapper: f'<iframe src="{inner}"></iframe>',
                    inner: ('<div class="chatbot-window-web__header">问答</div><div class="history_session_item is-active">进行中</div>'
                            '<div class="open-work-view-action"><div class="receive-box-inner">' + tutor +
                            '</div><div class="prompt-send-box"><textarea></textarea><button class="prompt-send-btn">发送</button></div></div>'
                            '<script>window.sends=0;document.querySelector("button").onclick=()=>{sends++;'
                            'document.querySelector(".receive-box-inner").insertAdjacentHTML("beforeend",'
                            '\'<div class="msg-item"><div class="user-sent-message"><p class="ai-text-message-send"></p></div></div>\');'
                            'document.querySelector(".ai-text-message-send").textContent=document.querySelector("textarea").value;'
                            'document.querySelector(".history_session_item").innerHTML=\'<span class="score-value">8分</span>\';'
                            'document.querySelector(".prompt-send-box").remove();};</script>')
                }
                await page.route(site + '/**', lambda r: r.fulfill(content_type='text/html; charset=utf-8',
                    body=pages.get(r.request.url[len(site):], '')))
                await page.goto(site + exercise)
                @asynccontextmanager
                async def attached():
                    yield SimpleNamespace(chromium=SimpleNamespace(connect_over_cdp=AsyncMock(return_value=browser)))
                monkeypatch.setattr(open_work, 'async_playwright', attached)
                answers = []
                def answer(course, title, rules, rows, *_):
                    answers.append((course, title, rows[-1]['text']))
                    return 'reply', '答案'
                monkeypatch.setattr(open_work, 'draft_reply', answer)
                args = dict(key='test', options={}, cancelled=lambda: False, event=lambda _:None,
                            store=Store(tmp_path), account='tester')
                result = await open_work.run_dialogue('', page.url, site, **args)
                assert '确认发送 1 轮' in result['text'] and '8分' in result['text']
                await open_work.run_dialogue('', page.url, site, **args)
                assert answers == [('统计学','问答','请回答')]
                assert await page.frames[-1].evaluate('window.sends') == 1
            finally:
                await browser.close()
    asyncio.run(run())
