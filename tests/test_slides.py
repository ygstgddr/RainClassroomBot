import asyncio
import time
import pytest
from core.store import Store
from core.errors import AdaptationRequired, Interrupted, RetryableError
from core.logging_setup import make_logger
from browser.edge_manager import EdgeManager
from browser.site_adapter import SiteAdapter
from task.scanner import scan
from task.executor import execute
from tests.mock_site import MockSite
from tests.test_edge import config_for


async def setup(site, tmp_path):
    site.include_slides = True
    config = config_for(tmp_path, site.url)
    store = Store(tmp_path)
    store.select_courses(['10'], '7')
    store.set('desired', 'scan')
    browser = EdgeManager(config)
    await browser.start()
    await browser.page.goto(site.url+'/login')
    adapter = SiteAdapter(browser, config)
    await scan(adapter, store, config, make_logger(tmp_path, 'slides'))
    task = next(t for t in store.rows() if t['kind']=='slides')
    store.set('desired', 'run')
    return config, store, browser, adapter, task


def test_ppt_reads_unfinished_pages_and_requires_server_evidence(tmp_path):
    async def scenario(site):
        site.slide_reads[0] = time.monotonic()-10
        config, store, browser, adapter, task = await setup(site, tmp_path)
        try:
            assert task['state']=='pending' and task['progress']==1/3
            evidence = await execute(task, adapter, browser, store, config)
            assert evidence and (await adapter.status(task))[0]
            assert set(site.slide_reads)=={0,1,2}
            assert browser.page.url==site.url+'/v2/web'
            assert not any(p.startswith('/observed-') and p not in ('/observed-slide/0','/observed-slide/1','/observed-slide/2') for m,p in site.requests if m=='POST')
        finally:
            await browser.close()
    with MockSite() as site:asyncio.run(scenario(site))


def test_pause_leaves_ppt_page_and_stops_page_recording(tmp_path):
    async def scenario(site):
        site.delay=100
        config, store, browser, adapter, task = await setup(site,tmp_path)
        try:
            running=asyncio.create_task(execute(task,adapter,browser,store,config))
            for _ in range(150):
                if store.get('current',{}).get('current_page'):break
                await asyncio.sleep(.1)
            else:pytest.fail('PPT reader did not start')
            store.set('desired','paused')
            with pytest.raises(Interrupted):await running
            assert browser.page.url==site.url+'/v2/web'
            before=len(site.slide_reads)
            await asyncio.sleep(.8)
            assert len(site.slide_reads)==before
            assert store.rows()[-1]['state']!='completed'
        finally:await browser.close()
    with MockSite() as site:asyncio.run(scenario(site))


@pytest.mark.parametrize('broken', [False, True])
def test_unrecorded_or_broken_ppt_does_not_advance_or_complete(tmp_path, broken):
    async def scenario(site):
        site.slides_broken=broken
        site.ignore_slide_reads=True
        config, store, browser, adapter, task=await setup(site,tmp_path)
        config.update(video_stall_seconds=1,confirmation_timeout_seconds=1)
        try:
            with pytest.raises(RetryableError,match='图片加载失败|服务器记录长期未更新'):
                await execute(task,adapter,browser,store,config)
            assert not site.slide_reads
            assert not any(p.endswith('/1') or p.endswith('/2') for m,p in site.requests if m=='POST')
            assert not (await adapter.status(task))[0]
        finally:await browser.close()
    with MockSite() as site:asyncio.run(scenario(site))


def test_ppt_with_questions_requires_manual_work(tmp_path):
    async def scenario(site):
        site.slides_have_problem=True
        config,store,browser,adapter,task=await setup(site,tmp_path)
        try:
            with pytest.raises(AdaptationRequired,match='答题页'):
                await execute(task,adapter,browser,store,config)
            assert not any(m=='POST' for m,p in site.requests)
        finally:await browser.close()
    with MockSite() as site:asyncio.run(scenario(site))


def test_executor_does_not_treat_homework_as_reading(tmp_path):
    store=Store(tmp_path);store.set('desired','run')
    with pytest.raises(AdaptationRequired):
        asyncio.run(execute({'kind':'homework'},None,None,store,{}))
