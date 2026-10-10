"""课程日期使用雨课堂 class_start/class_end，界面统一显示北京时间。"""
import math
import time
from datetime import datetime, timezone, timedelta

CHINA = timezone(timedelta(hours=8))


def timestamp(value):
    if isinstance(value, bool) or value is None:
        return None
    try:
        value = float(value)
        if value > 1e11:
            value /= 1000
        if not math.isfinite(value) or value <= 0:
            return None
        datetime.fromtimestamp(value, CHINA)
        return value
    except (ValueError, TypeError, OverflowError, OSError):
        return None


def date_text(value):
    value = timestamp(value)
    return datetime.fromtimestamp(value, CHINA).strftime('%Y-%m-%d %H:%M') if value else '未提供'


def course_state(course, now=None):
    now = time.time() if now is None else now
    start, end = timestamp(course.get('start')), timestamp(course.get('end'))
    if start and end and end < start:
        return '时间异常'
    if end and end < now:
        return '已结束'
    if start and start > now:
        return '未开课'
    if start and end:
        return '进行中'
    return '时间未知'
