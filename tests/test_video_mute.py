"""Mute only the assistant-owned browser and keep resumed/replaced media quiet."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from playwright.async_api import async_playwright

from browser.edge_manager import EdgeManager
from handler.video import VideoHandler
from tests.mock_site import VIDEO


@pytest.mark.parametrize('headed', [False, True])
def test_owned_edge_always_launches_with_audio_muted(tmp_path, monkeypatch, headed):
    async def scenario():
        page = SimpleNamespace(goto=AsyncMock())
        context = SimpleNamespace(pages=[page], set_default_timeout=Mock(),
            set_default_navigation_timeout=Mock(), close=AsyncMock())
        launch = AsyncMock(return_value=context)
        engine = SimpleNamespace(chromium=SimpleNamespace(launch_persistent_context=launch), stop=AsyncMock())
        monkeypatch.setattr('browser.edge_manager.async_playwright',
            lambda: SimpleNamespace(start=AsyncMock(return_value=engine)))
        config = dict(profile_dir=str(tmp_path/'owned-profile'), headless=True,
                      navigation_timeout_seconds=10, base_url='about:', entry_path='blank')
        manager = EdgeManager(config)
        await manager.start(headed=headed)
        assert launch.call_args.kwargs['channel'] == 'msedge'
        assert '--mute-audio' in launch.call_args.kwargs['args']
        assert launch.call_args.args == (str(tmp_path/'owned-profile'),)
        assert launch.call_args.kwargs['headless'] is (not headed)
        await manager.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('framed', [False, True])
def test_actual_video_initial_play_resume_and_replacement_are_muted(framed):
    """The existing local WebM fixture exercises real playback without an account."""
    async def scenario():
        async with async_playwright() as engine:
            browser = await engine.chromium.launch(channel='msedge', headless=True)
            try:
                page = await browser.new_page()
                if framed:
                    await page.set_content('<iframe style="width:640px;height:360px"></iframe>')
                    frame = page.frames[1]
                else:
                    frame = page
                handler = VideoHandler(page, {'auto_max_video_speed': False, 'video_stall_seconds': 30})
                try:
                    async def replace_player():
                        await frame.set_content(VIDEO.replace(
                            "video.addEventListener('ended',()=>fetch('/observed-ended',{method:'POST'}));",
                            "video.loop=true;window.playEvents=[];video.addEventListener('play',()=>window.playEvents.push(video.muted));"))
                        await frame.wait_for_function('document.querySelector("video").readyState >= 1')
                        return frame.locator('video')
                    video = await replace_player()
                    assert not await video.evaluate('v=>v.muted')
                    initial = await handler.tick()
                    await frame.wait_for_function('window.playEvents.length===1')
                    assert initial['muted'] and not initial['ready']
                    assert await frame.evaluate('window.playEvents') == [True]
                    await video.evaluate('v=>{v.pause();v.muted=false}')
                    resumed = await handler.tick()
                    await frame.wait_for_function('window.playEvents.length===2')
                    assert resumed['muted'] and not resumed['ready']
                    assert await frame.evaluate('window.playEvents') == [True, True]
                    # Websites may rebuild the element after navigation/load errors.
                    video = await replace_player()
                    await handler.tick()
                    await frame.wait_for_function('window.playEvents.length===1')
                    assert await frame.evaluate('window.playEvents') == [True]
                    assert await video.evaluate('v=>v.muted && v.defaultMuted')
                finally:
                    handler.close()
            finally:
                await browser.close()
    asyncio.run(asyncio.wait_for(scenario(), timeout=45))
