import asyncio
import json
import time
import pytest
from core.config import load_config
from core.store import Store
from core.errors import LoginRequired, Interrupted, RetryableError
from core.logging_setup import make_logger
from browser.edge_manager import EdgeManager
from browser.site_adapter import SiteAdapter
from task.scanner import scan
from task.executor import execute
from task.scheduler import run_queue
from tests.mock_site import MockSite


def config_for(tmp_path, url):
    config = load_config()
    config.update(root=str(tmp_path), profile_dir=str(tmp_path / 'profile'), base_url=url, headless=True,
                  poll_seconds=.3, video_stall_seconds=8, confirmation_timeout_seconds=8,
                  task_timeout_seconds=30, article_path_template='/article/{leaf_id}',
                  video_path_template='/video/{leaf_id}', reading_characters_per_minute=10000)
    return config


def test_real_edge_video_article_server_confirmation_and_login_persistence(tmp_path):
    async def scenario(site):
        config = config_for(tmp_path, site.url)
        store = Store(tmp_path)
        store.select_courses(['10'], '7')
        store.set('desired', 'scan')
        browser = EdgeManager(config)
        try:
            await browser.start()
            adapter = SiteAdapter(browser, config)
            with pytest.raises(LoginRequired):
                await adapter.account()
            await browser.page.goto(site.url + '/login')
            await scan(adapter, store, config, make_logger(tmp_path, 'test_edge'))
            assert len(store.rows()) == 2
            assert site.finished == {}
            # 完全关闭后重开相同 profile；cookie 仍有效。
            await browser.close()
            await browser.start()
            adapter = SiteAdapter(browser, config)
            assert (await adapter.account())['id'] == '7'
            site.fail_schedule = True
            with pytest.raises(RetryableError):
                await adapter.status(store.rows()[0])
            site.fail_schedule = False
            assert (await adapter.status(store.rows()[0]))[0] is False
            store.set('desired', 'run')
            await run_queue(adapter, browser, store, config, make_logger(tmp_path, 'test_edge'))
            tasks = store.rows()
            assert all(t['state'] == 'completed' for t in tasks)
            assert set(site.finished) == {'1', '2'}
            assert all(json.loads(t['evidence'])['value'] == 1 for t in tasks)
            assert all(t['updated'] >= time.time()-120 for t in tasks)
            assert site.status_reads >= 4
            assert store.get('desired') == 'idle'
            assert [(m,p) for m,p in site.requests if m == 'POST'] == [('POST','/observed-read'), ('POST','/observed-ended')]
        finally:
            await browser.close()
    with MockSite() as site:
        asyncio.run(scenario(site))


def test_pause_and_account_switch_do_not_complete(tmp_path):
    async def scenario(site):
        config = config_for(tmp_path, site.url)
        store = Store(tmp_path)
        store.select_courses(['10'], '7')
        browser = EdgeManager(config)
        try:
            await browser.start()
            await browser.page.goto(site.url + '/login')
            adapter = SiteAdapter(browser, config)
            store.set('desired', 'scan')
            await scan(adapter, store, config, make_logger(tmp_path, 'test_pause'))
            task = next(t for t in store.rows() if t['kind'] == 'video')
            store.set('desired', 'paused')
            with pytest.raises(Interrupted):
                await execute(task, adapter, browser, store, config)
            assert store.rows()[0]['state'] == 'pending'
            store.set('desired', 'run')
            running = asyncio.create_task(execute(task, adapter, browser, store, config))
            await asyncio.sleep(.2)
            await browser.page.wait_for_function("() => document.querySelector('video')?.currentTime > 0.1")
            store.set('desired', 'paused')
            with pytest.raises(Interrupted):
                await running
            assert await browser.page.locator('video').evaluate('v => v.paused')
            assert '1' not in site.finished
            site.user_id = 8
            with pytest.raises(LoginRequired):
                await adapter.status(task)
        finally:
            await browser.close()
    with MockSite() as site:
        asyncio.run(scenario(site))


def test_ended_video_without_server_confirmation_fails(tmp_path):
    async def scenario(site):
        config = config_for(tmp_path, site.url)
        config['confirmation_timeout_seconds'] = 1
        store = Store(tmp_path)
        store.select_courses(['10'], '7')
        browser = EdgeManager(config)
        try:
            await browser.start()
            await browser.page.goto(site.url + '/login')
            adapter = SiteAdapter(browser, config)
            store.set('desired', 'scan')
            await scan(adapter, store, config, make_logger(tmp_path, 'test_unconfirmed'))
            task = next(t for t in store.rows() if t['kind'] == 'video')
            store.set('desired', 'run')
            with pytest.raises(RetryableError, match='服务器未确认'):
                await execute(task, adapter, browser, store, config)
            assert '1' in site.finished
            assert all(t['state'] != 'completed' for t in store.rows())
        finally:
            await browser.close()
    with MockSite() as site:
        site.delay = 999
        asyncio.run(scenario(site))
