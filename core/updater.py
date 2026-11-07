"""Opt-in GitHub release updates; never mix update traffic with learning cookies.

Network work is bounded and runs in a daemon thread. Only a newer stable release
from the fixed repository and a SHA-256 verified Windows installer may launch.
No source archive is extracted over the installation or user data.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import threading
import time
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

REPOSITORY = 'https://github.com/ygstgddr/RainClassroomBot'
LATEST_API = 'https://api.github.com/repos/ygstgddr/RainClassroomBot/releases/latest'
MAX_INSTALLER = 1024 * 1024 * 1024
NETWORK_PHASES = {'checking', 'downloading'}


def version_key(value):
    if not isinstance(value, str) or not re.fullmatch(r'v?\d{1,6}\.\d{1,6}\.\d{1,6}', value):
        raise ValueError('版本号格式不支持，请前往发布页查看')
    return tuple(int(part) for part in value.removeprefix('v').split('.'))


def safe_url(url):
    parts = urlsplit(url)
    if (parts.scheme != 'https' or parts.hostname not in {
            'api.github.com', 'github.com', 'release-assets.githubusercontent.com',
            'objects.githubusercontent.com'} or parts.username or parts.password
            or parts.port not in (None, 443)):
        raise ValueError('更新下载地址不属于受支持的 GitHub 地址')
    return url


class _Redirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return super().redirect_request(req, fp, code, msg, headers, safe_url(newurl))


def open_url(url):
    request = Request(safe_url(url), headers={
        'User-Agent': 'RainClassroomBot-Updater', 'Accept': 'application/vnd.github+json'
        if url == LATEST_API else 'application/octet-stream'})
    return build_opener(_Redirect()).open(request, timeout=15)


class _Cancelled(Exception):
    pass


class UpdateManager:
    def __init__(self, current_version, root, *, install_supported=None, opener=None, cache=None):
        self.current_version, self.root = current_version, Path(root).resolve()
        self.install_supported = (os.name == 'nt' and bool(getattr(sys, 'frozen', False))
                                  if install_supported is None else install_supported)
        self._open = opener or open_url
        self.cache = Path(cache or Path(tempfile.gettempdir()) / 'RainClassroomBotUpdates')
        self._lock = threading.RLock()
        self._cancel = threading.Event()
        self._thread = None
        self._release = None
        self._installer = None
        self._digest = None
        self._state = dict(phase='idle', message='手动检查 GitHub 正式版，不会自动安装',
                           current=current_version, latest='', notes='', downloaded=0, total=0,
                           release_url=REPOSITORY + '/releases/latest')

    def snapshot(self):
        with self._lock:
            return dict(self._state, install_supported=self.install_supported,
                        can_download=bool(self._release) and self._state['phase'] in {'available', 'error'})

    def _set(self, **values):
        with self._lock:
            self._state.update(values)

    def _checkpoint(self):
        if self._cancel.is_set():
            raise _Cancelled()

    def _read(self, url, limit):
        self._checkpoint()
        with self._open(url) as response:
            content = response.read(limit + 1)
        self._checkpoint()
        if len(content) > limit:
            raise ValueError('更新信息超过大小限制')
        return content

    def _start(self, phase, operation):
        with self._lock:
            if self._state['phase'] == 'installing' or (self._thread and self._thread.is_alive()):
                raise ValueError('更新操作正在进行，请稍候')
            self._cancel.clear()
            self._set(phase=phase, message='正在检查更新…' if phase == 'checking' else '正在下载并校验安装包…')
            self._thread = threading.Thread(target=self._run, args=(operation,), daemon=True, name='RainUpdate')
            self._thread.start()

    def _run(self, operation):
        try:
            operation()
        except _Cancelled:
            self._set(phase='available' if self._release else 'idle', message='已取消，可稍后重试', downloaded=0)
        except HTTPError as exc:
            message = {403: 'GitHub 暂时限制访问，请稍后重试', 429: '检查过于频繁，请稍后重试',
                       404: '尚未找到正式版发布，请稍后重试'}.get(exc.code, '更新服务暂不可用，请稍后重试')
            self._set(phase='error', message=message)
        except ValueError as exc:
            self._set(phase='error', message=str(exc))
        except Exception:
            self._set(phase='error', message='更新失败，请检查网络、磁盘空间及写入权限后重试')

    def check(self):
        self._start('checking', self._check)

    def _check(self):
        self._release = self._installer = self._digest = None
        self._set(latest='', notes='', downloaded=0, total=0, release_url=REPOSITORY + '/releases/latest')
        release = json.loads(self._read(LATEST_API, 1024 * 1024))
        if not isinstance(release, dict) or release.get('draft') or release.get('prerelease'):
            raise ValueError('发布信息不是正式版，请稍后重试')
        tag = release.get('tag_name')
        latest = version_key(tag)
        current = version_key(self.current_version)
        version = tag.removeprefix('v')
        self._set(latest=version, notes=str(release.get('body') or '此版本暂无更新说明')[:20000],
                  release_url=f'{REPOSITORY}/releases/tag/{tag}')
        if latest <= current:
            self._set(phase='current', message='当前已是最新版本' if latest == current else '当前版本高于已发布正式版，无需更新')
            return
        name = f'RainClassroomBot-{version}-Windows-x64-Setup.exe'
        checksum_name = f'SHA256SUMS-{version}.txt'
        assets = release.get('assets') or []
        if not isinstance(assets, list):
            raise ValueError('发布附件信息无效')
        asset = next((a for a in assets if isinstance(a, dict) and a.get('name') == name), None)
        checksum = next((a for a in assets if isinstance(a, dict) and a.get('name') == checksum_name), None)
        if not asset:
            self._set(phase='unsupported', message='发现新版本，但尚未提供 Windows 安装包；可查看发布页')
            return
        prefix = f'{REPOSITORY}/releases/download/{tag}/'
        if asset.get('browser_download_url') != prefix + name:
            raise ValueError('安装包来源与项目发布地址不符')
        size = asset.get('size')
        if type(size) is not int or not 0 < size <= MAX_INSTALLER:
            raise ValueError('安装包大小无效或超过限制')
        digest = asset.get('digest') or ''
        if not re.fullmatch(r'sha256:[0-9a-fA-F]{64}', digest):
            digest = ''
        checksum_url = (prefix + checksum_name if checksum and
                        checksum.get('browser_download_url') == prefix + checksum_name else '')
        if not digest and not checksum_url:
            self._set(phase='unsupported', message='发布缺少 SHA-256 校验信息，暂不能在软件内安装')
            return
        self._release = dict(name=name, url=prefix + name, size=size,
                             digest=digest[7:].lower() if digest else '', checksum_url=checksum_url)
        self._set(phase='available', message='发现新版本，可下载后安装', total=size)

    def download(self):
        with self._lock:
            if not self._release:
                raise ValueError('请先检查更新，确认存在可用安装包')
            self._start('downloading', self._download)

    def _download(self):
        release = dict(self._release)
        self._installer = self._digest = None
        self._set(downloaded=0, total=release['size'])
        digest = release['digest']
        if not digest:
            manifest = self._read(release['checksum_url'], 65536).decode('utf-8-sig')
            matches = [m.group(1).lower() for line in manifest.splitlines()
                       if (m := re.fullmatch(r'([0-9a-fA-F]{64})\s+\*?' + re.escape(release['name']), line))]
            if len(matches) != 1:
                raise ValueError('校验文件缺失安装包记录或存在歧义，已停止下载')
            digest = matches[0]
        self._checkpoint()
        self.cache.mkdir(parents=True, exist_ok=True)
        # Unique directory avoids consuming or replacing another app instance's file.
        folder = Path(tempfile.mkdtemp(prefix='release-', dir=self.cache))
        partial = folder / (release['name'] + '.part')
        target = folder / release['name']
        try:
            calculated, received, started = hashlib.sha256(), 0, time.monotonic()
            with self._open(release['url']) as response, partial.open('xb') as output:
                while True:
                    self._checkpoint()
                    if time.monotonic() - started > 1200:
                        raise ValueError('下载超时，请检查网络后重试')
                    chunk = response.read(256 * 1024)
                    if not chunk:
                        break
                    received += len(chunk)
                    if received > release['size']:
                        raise ValueError('下载大小与发布信息不一致，已停止')
                    output.write(chunk)
                    calculated.update(chunk)
                    self._set(downloaded=received)
            self._checkpoint()
            if received != release['size'] or calculated.hexdigest() != digest:
                raise ValueError('安装包不完整或 SHA-256 校验失败，请重新下载')
            partial.replace(target)
            self._installer, self._digest = target, digest
            self._set(phase='ready', message='安装包校验通过。安装会退出工作台，保留登录与任务记录')
        finally:
            partial.unlink(missing_ok=True)
            if not target.exists():
                folder.rmdir()

    def cancel(self):
        with self._lock:
            if self._state['phase'] in NETWORK_PHASES:
                self._cancel.set()
                self._set(message='正在取消，请稍候…')

    def reserve_install(self):
        """Mark installing before the desktop releases its command lock."""
        with self._lock:
            if not self.install_supported:
                raise ValueError('源码/演示模式不自动覆盖安装，请在 Windows 安装版中使用')
            if self._state['phase'] != 'ready' or (self._thread and self._thread.is_alive()):
                raise ValueError('请先等待安装包下载和校验完成')
            self._set(phase='installing', message='正在保存状态并启动安装程序…')

    def launch_installer(self):
        """Called only after the owned worker has stopped, before window teardown."""
        try:
            with self._lock:
                if self._state['phase'] != 'installing' or not self.install_supported:
                    raise ValueError('未确认安装更新')
                installer = self._installer
                if not installer or not installer.is_file():
                    raise ValueError('安装包已被移除，请重新下载')
                with installer.open('rb') as stream:
                    if hashlib.file_digest(stream, 'sha256').hexdigest() != self._digest:
                        raise ValueError('安装包发生变化，请重新下载')
                # Explicit current directory handles portable installations as well.
                # Interactive Setup retains cancellation/errors; never reboot or force-kill.
                subprocess.Popen([str(installer), '/SP-', '/NORESTART', '/NOFORCECLOSEAPPLICATIONS',
                                  '/NOCLOSEAPPLICATIONS', f'/DIR={self.root}'], cwd=str(installer.parent),
                                 close_fds=True)
        except Exception:
            self._set(phase='error', message='无法启动安装程序，请重新下载或从发布页手动安装')
            raise
