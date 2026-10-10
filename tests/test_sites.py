import asyncio
import json
import time
import tkinter as tk
from pathlib import Path

import pytest

from core.config import load_config
from core.sites import normalize_site, site_config, read_sites, save_site, prepare_site, PRESETS
from core.store import Store
from core.logging_setup import make_logger, record
from browser.edge_manager import EdgeManager
from gui.interface import Interface
from recovery.watchdog import Watchdog
from tests.mock_site import MockSite
from tests.test_core import sample


def config_at(root):
    config = load_config()
    config.update(root=str(root), profile_dir=str(root / 'edge_profile'), headless=True)
    return config


@pytest.mark.parametrize('value,expected', [
    (' changjiang.yuketang.cn ', PRESETS[1]),
    ('https://CHANGJIANG.yuketang.cn:443/web/', PRESETS[1]),
    ('www.yuketang.cn', PRESETS[0]),
    ('https://school.example.edu/v2/web', 'https://school.example.edu/v2/web'),
])
def test_normalize_site(value, expected):
    assert normalize_site(value) == expected


@pytest.mark.parametrize('value', [
    'file:///C:/test', 'javascript:alert(1)', 'https://x:y@example.com/web',
    'https://example.com/web?token=secret', 'https://example.com/web#key',
    'http://example.com/web', 'https://example.com/course/1', 'https://a..com',
])
def test_reject_non_homepage_and_credential_urls(value):
    with pytest.raises(ValueError):
        normalize_site(value)


def test_isolated_same_ids_and_persistent_selection(tmp_path):
    config = config_at(tmp_path)
    original = site_config(config, PRESETS[0])
    other = site_config(config, PRESETS[1])
    assert original['root'] == str(tmp_path)
    assert original['profile_dir'] == str(tmp_path / 'edge_profile')
    assert other['profile_dir'] != original['profile_dir']
    assert other['article_path_template'] == ''
    first, second = Store(original['root']), Store(other['root'])
    first.set('account', {'id': 'a'})
    first.select_courses(['10'])
    first.merge_course('a', '10', [sample()])
    first.update('a:10:1', state='completed', evidence='server-first')
    second.set('account', {'id': 'a'})
    second.merge_course('a', '10', [sample()])
    assert second.selected_courses() == []
    assert second.rows()[0]['state'] == 'pending'
    save_site(config, PRESETS[1])
    assert read_sites(config)[0] == PRESETS[1]
    save_site(config, PRESETS[0])
    restored = Store(site_config(config, read_sites(config)[0])['root'])
    assert restored.selected_courses() == ['10']
    assert restored.rows()[0]['evidence'] == 'server-first'
    assert not (tmp_path / 'config/site.tmp').exists()
    # 日志也按站点写入，不能复用旧 logger 的文件句柄。
    for cfg, store, label in ((original, first, 'first'), (other, second, 'second')):
        record(store, make_logger(cfg['root'], 'sites_test'), label)
    assert 'second' not in (tmp_path / 'logs/sites_test.jsonl').read_text(encoding='utf8')


def test_gui_invalid_url_and_close_during_switch(tmp_path):
    config = config_at(tmp_path)
    store = Store(tmp_path)
    class Dog:
        alive = True
        stopped = False
        def stop(self): self.stopped = True
        def is_alive(self): return self.alive
    dog = Dog()
    root = tk.Tk();root.withdraw()
    try:
        ui = Interface(root, store, dog, base_config=config)
        ui.site_address.set('https://example.com/web?secret=value')
        ui.switch_site()
        assert not dog.stopped and not ui.switching
        ui.site_address.set(PRESETS[1])
        ui.switch_site()
        assert dog.stopped and ui.switching
        ui.command('run')
        assert store.get('desired') != 'run'
        assert not (tmp_path / 'sites/changjiang.yuketang.cn/data/state.sqlite3').exists()
        ui.closing = True
        dog.alive = False
        ui.finish_site_switch(PRESETS[1], site_config(config, PRESETS[1]))
        assert read_sites(config)[0] == PRESETS[0]
    finally:
        root.destroy()


def test_gui_real_edge_switch_back_preserves_site_data(tmp_path):
    with MockSite() as first_site, MockSite() as second_site:
        config = config_at(tmp_path)
        config.update(base_url=first_site.url, entry_path='/v2/web')
        first_url, second_url = first_site.url + '/v2/web', second_site.url + '/web'
        async def login():
            for url in (first_url, second_url):
                cfg = site_config(config, url)
                browser = EdgeManager(cfg)
                try:
                    await browser.start()
                    await browser.page.goto(cfg['base_url'] + '/login')
                finally:
                    await browser.close()
        asyncio.run(login())
        store = prepare_site(site_config(config, first_url))
        store.merge_course('7', '10', [sample('7')])
        store.select_courses(['10'], '7')
        dog = Watchdog(site_config(config, first_url));dog.start()
        root = tk.Tk();root.withdraw()
        ui = Interface(root, store, dog, base_config=config)
        def wait(predicate):
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                root.update()
                if predicate(): return
                time.sleep(.05)
            raise AssertionError('网站切换或扫描超时')
        try:
            wait(lambda: store.get('desired') == 'idle')
            first_pid = store.get('worker_pid')
            ui.site_address.set(second_url);ui.switch_site()
            wait(lambda: not ui.switching and ui.store.get('desired') == 'idle')
            assert not dog.is_alive()
            assert ui.active_site == second_url
            assert ui.store.get('account')['id'] == '7'
            assert len(ui.store.catalog()) == 1
            assert ui.store.selected_courses() == [] and ui.store.rows() == []
            assert ui.store.get('worker_pid') != first_pid
            assert read_sites(config)[0] == second_url
            ui.site_address.set(first_url);ui.switch_site()
            wait(lambda: not ui.switching and ui.store.get('desired') == 'idle')
            assert ui.store.path == store.path
            assert ui.store.selected_courses() == ['10']
            assert len(ui.store.rows()) == 1
            assert not ui.store.scope_scanned()
        finally:
            ui.closing = True
            ui.watchdog.stop();ui.watchdog.join(20)
            root.destroy()
        assert not ui.watchdog.is_alive()
