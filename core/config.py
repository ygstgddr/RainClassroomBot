from pathlib import Path
import sys
import yaml


def app_root():
    return Path(sys.executable).parent if getattr(sys, 'frozen', False) else Path(__file__).resolve().parents[1]


def load_config(root=None):
    root = Path(root or app_root()).resolve()
    with (root / 'config' / 'config.yaml').open(encoding='utf-8') as stream:
        config = yaml.safe_load(stream)
    config['root'] = str(root)
    # 旧安装保留原配置文件时，也默认启用本次新增功能。
    config.setdefault('auto_max_video_speed', True)
    config.setdefault('slides_page_seconds', 5)
    if (type(config['slides_page_seconds']) not in (int, float)
            or not 1 <= config['slides_page_seconds'] <= 120):
        raise ValueError('配置 slides_page_seconds 必须在 1 到 120 秒之间')
    if not isinstance(config['auto_max_video_speed'], bool):
        raise ValueError('配置 auto_max_video_speed 必须为 true 或 false')
    config['profile_dir'] = str((root / config['profile_dir']).resolve())
    for key in ('poll_seconds', 'scan_concurrency', 'max_task_attempts',
                'watchdog_timeout_seconds', 'request_timeout_seconds',
                'navigation_timeout_seconds', 'video_stall_seconds',
                'confirmation_timeout_seconds', 'task_timeout_seconds',
                'max_restarts_per_hour', 'reading_characters_per_minute'):
        if not isinstance(config.get(key), (int, float)) or config[key] <= 0:
            raise ValueError(f'配置 {key} 必须大于 0')
    if config['scan_concurrency'] > 4:
        raise ValueError('扫描并发最多为 4，建议 2')
    if config['watchdog_timeout_seconds'] <= config['request_timeout_seconds']:
        raise ValueError('守护超时必须大于单次请求超时')
    return config
