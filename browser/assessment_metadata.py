"""Assessment facts explicitly published in the chapter or on the landing page."""
import re
from core.courses import timestamp


# Shared with the page reader so a task has the same eligibility at either entry.
FILE_REQUIREMENT = (r'(?:请|需要|必须|务必|要求|仅支持|须|需)[^。，；;\n]{0,16}?(?:上传|提交)'
                    r'[^。，；;\n]{0,8}?(?:文件|附件|实验报告|PDF|文档)|以附件形式提交|'
                    r'(?:提交方式|作答方式)\s*[：:]?\s*(?:附件|文件)上传')
OPTIONAL_FILE = r'(?:可(?:以)?(?:选择|另外|另行)?(?:上传|提交))|(?:无需|无须|不需要|不要求|不必|不须|可选|可自行|按需|如有需要)[^。，；;\n]{0,16}?(?:上传|提交)'


def requires_file(text):
    return bool(re.search(FILE_REQUIREMENT, re.sub(OPTIONAL_FILE, '', text), re.I))


def landing_details(text):
    info = {}
    if requires_file(text):
        info['requires_file'] = True
    limit = re.search(r'(?:考试限时|答题时限|考试时长|答题限时)\s*[：:]?\s*(不限时|无时间限制|不限|\d+\s*(?:分钟|小时|秒)(?:\s*\d+\s*分钟)?)', text)
    if limit:
        info['time_limit'] = limit.group(1)
    end = re.search(r'(?:考试截止时间|截止时间)\s*[：:]?\s*(\d{4}-\d{2}-\d{2})\s*(?:/\s*)?(\d{2}:\d{2}(?::\d{2})?)', text)
    if end:
        value = timestamp(' '.join(end.groups()))
        if value:
            info['task_deadline'] = value
    elif re.search(r'(?:考试截止时间|截止时间)\s*[：:]\s*(?:无|不限|未设置)(?:\s|$)', text):
        info['no_deadline'] = True
    return info
