import asyncio

import pytest
from playwright.async_api import async_playwright

from handler.video_speed import NativeVideoSpeed


def player(legacy=False, broken=False):
    if legacy:
        menu = '''<xt-speedbutton>倍速<ul><li data-speed="1">1x</li>
          <li data-speed="3">3x</li><li data-speed="2">2x</li>
          <li data-speed="4" aria-disabled="true">4x</li></ul></xt-speedbutton>'''
        selector, value = 'li[data-speed]', 'Number(e.dataset.speed)'
    else:
        menu = '''<div class="rate-btn-box">倍速<div class="rate-config-box">
          <div class="rate-config-item">1x</div><div class="rate-config-item">2x</div>
          <div class="rate-config-item">1.5x</div>
          <div class="rate-config-item disabled">8x</div></div></div>'''
        selector, value = '.rate-config-item', 'parseFloat(e.textContent)'
    action = '' if broken else f"document.querySelector('video').playbackRate={value};"
    guard = '''let moved=false,x=0,y=0;
      const trigger=document.querySelector('xt-speedbutton');
      trigger.addEventListener('mouseover',e=>{x=e.clientX;y=e.clientY;moved=false});
      trigger.addEventListener('mousemove',e=>{moved=Math.hypot(e.clientX-x,e.clientY-y)>0});
      trigger.addEventListener('mouseout',()=>{moved=false});''' if legacy else ''
    if legacy:
        action = 'if(!moved)return;' + action
    return f'''<style>video {{width:320px;height:180px;background:#333}}
      .rate-config-box,ul {{display:none}} .rate-btn-box:hover .rate-config-box,
      xt-speedbutton:hover ul {{display:block}} xt-speedbutton {{display:block}}
      </style><div id="player"><video></video>{menu}</div>
      <script>(()=>{{window.clicks=0;{guard}document.querySelectorAll('{selector}').forEach(e=>
      e.onclick=()=>{{window.clicks++;{action}}});}})();</script>'''


@pytest.mark.parametrize('legacy,framed', [(False, False), (True, False), (False, True)])
def test_native_maximum_reset_and_reload(legacy, framed):
    async def scenario():
        async with async_playwright() as p:
            browser = await p.chromium.launch(channel='msedge', headless=True)
            try:
                page = await browser.new_page()
                if framed:
                    await page.set_content('<iframe style="width:600px;height:500px"></iframe>')
                    frame = page.frames[1]
                else:
                    frame = page
                await frame.set_content(player(legacy))
                helper = NativeVideoSpeed(retry_seconds=0)
                video = frame.locator('video')
                expected = 3 if legacy else 2
                result = await helper.ensure(video)
                assert result['speed_confirmed'] and result['playback_rate'] == expected
                assert await frame.evaluate('window.clicks') == 1
                await frame.evaluate("window.moves=0;document.addEventListener('mousemove',()=>window.moves++)")
                # 已处于最高倍速时不重复点击。
                await helper.ensure(video)
                assert await frame.evaluate('window.clicks') == 1
                assert await frame.evaluate('window.moves') == 0
                await video.evaluate('v=>v.playbackRate=1')
                assert (await helper.ensure(video))['playback_rate'] == expected
                assert await frame.evaluate('window.clicks') == 2
                # 同一处理器遇到重建的播放器仍会重新探测。
                await frame.set_content(player(legacy))
                assert (await helper.ensure(frame.locator('video')))['speed_confirmed']
            finally:
                await browser.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('case', ['no_menu', 'disabled_setting', 'broken', 'other_player'])
def test_no_false_confirmation_or_unrelated_clicks(case):
    async def scenario():
        async with async_playwright() as p:
            browser = await p.chromium.launch(channel='msedge', headless=True)
            try:
                page = await browser.new_page()
                html = player(broken=case == 'broken')
                if case == 'no_menu':
                    html = '<video style="width:300px;height:180px"></video>'
                elif case == 'other_player':
                    html += '<div><video id="target" style="width:300px;height:180px"></video></div>'
                await page.set_content(html)
                video = page.locator('#target' if case == 'other_player' else 'video')
                result = await NativeVideoSpeed(enabled=case != 'disabled_setting').ensure(video)
                assert not result['speed_confirmed']
                assert result['playback_rate'] == 1
                if case in ('other_player', 'disabled_setting'):
                    assert await page.evaluate('window.clicks') == 0
                if case == 'broken':
                    assert result['max_playback_rate'] == 2
            finally:
                await browser.close()
    asyncio.run(scenario())


def test_parse_menu_rate():
    assert NativeVideoSpeed.parse_rate(' 2.50X ') == 2.5
    assert NativeVideoSpeed.parse_rate('1.25倍') == 1.25
    for value in ('NaN', 'Infinity', '-2x', '0x', '自动', '最高16倍速'):
        assert NativeVideoSpeed.parse_rate(value) is None
