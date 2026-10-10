import json
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
from datetime import datetime


def make_logger(root, name='worker'):
    folder = Path(root) / 'logs'
    folder.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger(f'{name}:{folder.resolve()}')
    logger.setLevel(logging.INFO)
    if not logger.handlers:
        handler = RotatingFileHandler(folder / f'{name}.jsonl', maxBytes=5_000_000,
                                      backupCount=8, encoding='utf-8')
        handler.setFormatter(logging.Formatter('%(message)s'))
        logger.addHandler(handler)
    return logger


def record(store, logger, status, reason='', task=None):
    task = task or {}
    store.event(status, reason, task)
    logger.info(json.dumps(dict(time=datetime.now().astimezone().isoformat(),
                               course=task.get('course', ''), task=task.get('title', ''),
                               status=status, reason=reason), ensure_ascii=False))
