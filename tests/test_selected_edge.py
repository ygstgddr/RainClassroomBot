"""Real local server + Edge: selecting one item must not visit its neighbor."""
import asyncio
import pytest
from browser.edge_manager import EdgeManager
from browser.site_adapter import SiteAdapter
from core.logging_setup import make_logger
from core.store import Store
from core.workflow import Workflow
from gui.web_interface import WebInterface
from tests.mock_site import MockSite
from tests.test_edge import config_for


@pytest.mark.parametrize('rescan', [False, True])
def test_explicit_selection_survives_real_scan_and_server_confirmation(tmp_path, rescan):
    async def scenario(site):
        config = config_for(tmp_path, site.url)
        browser = EdgeManager(config)
        await browser.start()
        try:
            await browser.page.goto(site.url + '/login')
            store = Store(tmp_path)
            store.set('account', {'id': '7'}); store.select_courses(['10'])
            store.set('desired', 'scan')
            flow = Workflow(SiteAdapter(browser, config), browser, store, config, make_logger(tmp_path, 'selected-edge'))
            await flow.perform('scan')
            assert len(store.rows()) == 2
            if rescan:
                store.set('scanned_account', None)
            assert WebInterface(store).action('start_tasks', {'ids': ['7:10:2']})['ok']
            await flow.perform('run')
            assert store.get('desired') == 'idle'
            assert set(site.finished) == {'2'}
            assert ('GET', '/video/1') not in site.requests
            assert [r for r in site.requests if r[0]=='POST'] == [('POST', '/observed-read')]
            assert next(t for t in store.rows() if t['leaf_id']=='2')['evidence']
            assert next(t for t in store.rows() if t['leaf_id']=='1')['state'] == 'pending'
        finally:
            await browser.close()
    with MockSite() as site:
        asyncio.run(scenario(site))
