"""站点地址、独立数据目录和原子保存的站点选择。原有主站目录不迁移。"""
import json
import os
import re
import time
from pathlib import Path
from urllib.parse import urlsplit

from core.store import Store

PRESETS = ('https://www.yuketang.cn/v2/web', 'https://changjiang.yuketang.cn/web')


def normalize_site(value):
    value = value.strip()
    if '://' not in value:
        value = 'https://' + value
    try:
        parsed = urlsplit(value)
        host = (parsed.hostname or '').lower()
        port = parsed.port
    except ValueError as exc:
        raise ValueError('网站地址格式不正确') from exc
    if (parsed.scheme not in ('https', 'http') or not host or parsed.username is not None
            or parsed.password is not None or parsed.query or parsed.fragment
            or not re.fullmatch(r'[a-z0-9]+(?:[a-z0-9.-]*[a-z0-9])?', host)
            or '..' in host):
        raise ValueError('请输入雨课堂首页地址，不包含账号、查询参数或片段')
    if parsed.scheme == 'http' and host not in ('localhost', '127.0.0.1'):
        raise ValueError('雨课堂网站地址需要使用 https://')
    if port is not None and not 1 <= port <= 65535:
        raise ValueError('网站端口无效')
    path = parsed.path.rstrip('/')
    if path not in ('', '/web', '/v2/web'):
        raise ValueError('请填写网站首页（/web 或 /v2/web），不要填写具体课程链接')
    path = path or ('/v2/web' if host == 'www.yuketang.cn' else '/web')
    default_port = 443 if parsed.scheme == 'https' else 80
    origin = f'{parsed.scheme}://{host}' + (f':{port}' if port and port != default_port else '')
    return origin + path


def settings_path(base_config):
    return Path(base_config['root']) / 'config' / 'site.json'


def read_sites(base_config):
    default = normalize_site(base_config['base_url'] + base_config['entry_path'])
    path = settings_path(base_config)
    data = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
    selected = normalize_site(data.get('selected', default))
    history = []
    for value in [selected, default, *PRESETS, *data.get('history', [])]:
        normalized = normalize_site(value)
        if normalized not in history:
            history.append(normalized)
    return selected, history[:20]


def save_site(base_config, value):
    selected = normalize_site(value)
    _, history = read_sites(base_config)
    history = [selected, *[v for v in history if v != selected]][:20]
    path = settings_path(base_config)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.tmp')
    # 不改写 config.yaml，保留用户现有配置和注释。
    with temporary.open('w', encoding='utf-8') as stream:
        json.dump({'selected': selected, 'history': history}, stream, ensure_ascii=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def site_config(base_config, value):
    url = normalize_site(value)
    parsed = urlsplit(url)
    origin = f'{parsed.scheme}://{parsed.netloc}'
    result = dict(base_config, base_url=origin, entry_path=parsed.path, site_url=url)
    legacy_origin = urlsplit(normalize_site(base_config['base_url'] + base_config['entry_path']))
    if origin != f'{legacy_origin.scheme}://{legacy_origin.netloc}':
        key = parsed.hostname + (f'-port-{parsed.port}' if parsed.port else '')
        if parsed.scheme != 'https':
            key = parsed.scheme + '-' + key
        root = (Path(base_config['root']) / 'sites' / key).resolve()
        result.update(root=str(root), profile_dir=str(root / 'edge_profile'))
        # 任务直达模板可能属于旧站点；跨站点用新站正常课程页面定位。
        result['video_path_template'] = ''
        result['article_path_template'] = ''
    return result


def prepare_site(config):
    store = Store(config['root'])
    store.recover()
    store.set('current', {})
    store.set('scanned_account', None)
    store.set('desired', 'catalog')
    store.set('worker_busy', False)
    store.set('force_headed', False)
    store.set('worker_status', '正在打开网站，读取课程列表')
    store.set('scan_progress', dict(status='queued', mode='catalog', phase='正在启动 Edge，读取课程列表',
                                    total=0, completed=0, started=time.time()))
    return store
