"""雨课堂章节类型映射，与网页 leaf_type 定义一致。

展示类型与执行能力分离：题目/作业不会落入阅读处理器。
"""
TYPES = {
    0: ('video', '视频观看'),
    1: ('audio', '音频收听'),
    2: ('live', '直播学习'),
    3: ('article', '图文阅读'),
    4: ('discussion', '讨论互动'),
    5: ('exam', '考试'),
    6: ('homework', '作业提交'),
    7: ('slides', 'PPT / 课件阅读'),
    8: ('classroom', '课堂 / 回放'),
    9: ('quiz', '试卷测验'),
    10: ('notice', '公告阅读'),
}
AUTOMATED_KINDS = frozenset(('video', 'article', 'slides'))


def task_kind(leaf_type):
    return TYPES.get(leaf_type, ('unknown', '未知类型'))[0]


def type_label(task):
    """也能显示旧数据库中的 manual 任务；不必等重扫才修正类型名称。"""
    return TYPES.get(task['leaf_type'], ('unknown', f"未知类型 ({task['leaf_type']})"))[1]


def manual_note(leaf_type):
    if leaf_type in (5, 6, 9):
        return '需要作答或上传内容，请在网页完成后重新扫描'
    if leaf_type == 8:
        return '课堂记录可能含签到、互动或回放，需在网页核对'
    return '该类型暂未接入自动操作，请在网页处理后重新扫描'
