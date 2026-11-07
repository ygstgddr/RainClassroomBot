"""Release/update failure cases without executing downloaded programs."""
import hashlib
import io
import json
import threading
from urllib.error import HTTPError

import pytest

from core.updater import LATEST_API, REPOSITORY, UpdateManager, safe_url, version_key
from gui.web_interface import WebInterface
from tests.test_web_bridge import FakeDog, ready_store, wait

CONTENT = b'MZ fictional test installer - never executed'


def release(version='0.9.0', digest=True):
    name = f'RainClassroomBot-{version}-Windows-x64-Setup.exe'
    prefix = f'{REPOSITORY}/releases/download/v{version}/'
    assets = [dict(name=name, browser_download_url=prefix + name, size=len(CONTENT))]
    if digest:
        assets[0]['digest'] = 'sha256:' + hashlib.sha256(CONTENT).hexdigest()
    assets.append(dict(name=f'SHA256SUMS-{version}.txt', browser_download_url=prefix + f'SHA256SUMS-{version}.txt'))
    return dict(tag_name='v' + version, draft=False, prerelease=False, assets=assets, body='修复与更新说明')


def manager(tmp_path, data=None, content=CONTENT, **kwargs):
    data = data or release()
    requests = []
    def opener(url):
        requests.append(url)
        if url == LATEST_API:
            return io.BytesIO(json.dumps(data).encode())
        if url.endswith('.txt'):
            manifest = hashlib.sha256(CONTENT).hexdigest() + '  ' + data['assets'][0]['name'] + '\n'
            return io.BytesIO(manifest.encode())
        return io.BytesIO(content)
    service = UpdateManager('0.8.7', tmp_path/'安装目录 with spaces', opener=opener,
                            cache=tmp_path/'cache', install_supported=True, **kwargs)
    return service, requests


def done(service):
    service._thread.join(5)
    assert not service._thread.is_alive()
    return service.snapshot()


@pytest.mark.parametrize('version,expected', [('0.8.6', 'current'), ('0.8.7', 'current'), ('0.10.0', 'available')])
def test_numeric_version_comparison(tmp_path, version, expected):
    service, requests = manager(tmp_path, release(version))
    assert requests == []  # Constructor/startup never calls GitHub.
    service.check()
    assert done(service)['phase'] == expected
    assert requests == [LATEST_API]


@pytest.mark.parametrize('value', ['0.9', 'v0.9.0-beta', '../../x', None])
def test_reject_unstable_or_invalid_versions(value):
    with pytest.raises(ValueError):
        version_key(value)


@pytest.mark.parametrize('url', ['http://github.com/x', 'https://evil.test/x',
    'https://github.com.evil.test/x', 'https://user@github.com/x', 'https://github.com:4433/x'])
def test_reject_untrusted_download_redirects(url):
    with pytest.raises(ValueError):
        safe_url(url)


@pytest.mark.parametrize('mutation,expected', [
    ('draft', 'error'), ('prerelease', 'error'), ('missing', 'unsupported'),
    ('unverified', 'unsupported'), ('other_repo', 'error'), ('size', 'error')])
def test_release_validation(tmp_path, mutation, expected):
    data = release()
    if mutation in {'draft', 'prerelease'}:
        data[mutation] = True
    elif mutation == 'missing':
        data['assets'] = []
    elif mutation == 'unverified':
        data['assets'] = [dict(data['assets'][0], digest='')]
    elif mutation == 'other_repo':
        data['assets'][0]['browser_download_url'] = 'https://github.com/elsewhere/app/releases/download/x.exe'
    else:
        data['assets'][0]['size'] = -1
    service, _ = manager(tmp_path, data)
    service.check()
    assert done(service)['phase'] == expected
    with pytest.raises(ValueError):
        service.download()


@pytest.mark.parametrize('digest', [True, False])
def test_verified_download_and_safe_installer_arguments(tmp_path, monkeypatch, digest):
    service, _ = manager(tmp_path, release(digest=digest))
    service.check(); done(service)
    service.download()
    assert done(service)['phase'] == 'ready'
    assert service._installer.read_bytes() == CONTENT
    launches = []
    monkeypatch.setattr('core.updater.subprocess.Popen', lambda args, **kwargs: launches.append((args, kwargs)))
    service.reserve_install()
    service.launch_installer()
    args, kwargs = launches[0]
    assert args[-1] == f'/DIR={service.root}'
    assert '/NORESTART' in args and '/NOFORCECLOSEAPPLICATIONS' in args
    assert '/NOCLOSEAPPLICATIONS' in args
    assert 'shell' not in kwargs


@pytest.mark.parametrize('content', [CONTENT[:-2], CONTENT+b'excess', b'X'*len(CONTENT)])
def test_corrupt_download_never_ready_and_partial_removed(tmp_path, content):
    service, _ = manager(tmp_path, content=content)
    service.check(); done(service)
    service.download()
    assert done(service)['phase'] == 'error'
    assert not list((tmp_path/'cache').rglob('*.part'))
    assert not list((tmp_path/'cache').rglob('*.exe'))
    with pytest.raises(ValueError):
        service.reserve_install()


def test_recheck_integrity_before_launch(tmp_path, monkeypatch):
    service, _ = manager(tmp_path)
    service.check(); done(service)
    service.download(); done(service)
    service._installer.write_bytes(b'tampered')
    monkeypatch.setattr('core.updater.subprocess.Popen', lambda *a, **kw: pytest.fail('must not launch'))
    service.reserve_install()
    with pytest.raises(ValueError):
        service.launch_installer()
    assert service.snapshot()['phase'] == 'error'


def test_cancel_and_duplicate_download_are_bounded(tmp_path):
    service, _ = manager(tmp_path)
    service.check(); done(service)
    entered, release_read = threading.Event(), threading.Event()
    class Slow(io.BytesIO):
        def read(self, n=-1):
            entered.set()
            assert release_read.wait(3)
            return super().read(n)
    service._open = lambda url: Slow(CONTENT)
    service.download()
    assert entered.wait(3)
    with pytest.raises(ValueError):
        service.download()
    service.cancel()
    release_read.set()
    assert done(service)['phase'] == 'available'
    assert not list((tmp_path/'cache').rglob('*.part'))
    assert not list((tmp_path/'cache').rglob('*.exe'))


def test_rate_limit_does_not_expose_network_details(tmp_path):
    service, _ = manager(tmp_path)
    def denied(url):
        raise HTTPError(url, 403, 'private proxy error', {}, None)
    service._open = denied
    service.check()
    state = done(service)
    assert state['phase'] == 'error' and 'GitHub' in state['message']
    assert 'private' not in state['message']


def test_install_waits_for_owned_worker_and_blocks_new_commands(tmp_path, monkeypatch):
    service, _ = manager(tmp_path)
    service.check(); done(service)
    service.download(); done(service)
    stopped = threading.Event()
    dog = FakeDog(release=stopped)
    ui = WebInterface(ready_store(tmp_path/'db'), dog)
    ui.updater = service
    stop = dog.stop
    def shutdown():
        ui.store.set('desired', 'shutdown')
        stop()
    dog.stop = shutdown
    launches = []
    monkeypatch.setattr('core.updater.subprocess.Popen', lambda *a, **kw: launches.append(not dog.is_alive()))
    assert ui.action('update_install')['ok']
    assert dog.joining.wait(3)
    assert launches == []
    assert not ui.action('start')['ok']
    stopped.set()
    wait(lambda: ui.closed)
    assert launches == [True]
    assert ui.store.get('desired') == 'paused'


def test_busy_install_and_custom_payloads_rejected(tmp_path):
    service, requests = manager(tmp_path)
    ui = WebInterface(ready_store(tmp_path/'db'))
    ui.updater = service
    ui.store.set('worker_busy', True)
    assert not ui.action('update_install')['ok']
    assert not ui.action('update_check', {'url': 'https://evil.test'})['ok']
    assert requests == []
    assert ui.action('update_check')['ok']  # Checking is allowed during learning.
    done(service)


def test_launch_failure_leaves_desktop_open(tmp_path, monkeypatch):
    service, _ = manager(tmp_path)
    service.check(); done(service)
    service.download(); done(service)
    ui = WebInterface(ready_store(tmp_path/'db'))
    ui.updater = service
    def fail(*a, **kw):
        raise OSError('blocked')
    monkeypatch.setattr('core.updater.subprocess.Popen', fail)
    assert ui.action('update_install')['ok']
    wait(lambda: not ui.closing)
    assert not ui.closed
    assert service.snapshot()['phase'] == 'error'


def test_source_mode_does_not_run_installer(tmp_path):
    service, _ = manager(tmp_path)
    service.install_supported = False
    service.check(); done(service)
    service.download(); done(service)
    with pytest.raises(ValueError, match='源码'):
        service.reserve_install()
