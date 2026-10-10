class LoginRequired(Exception):
    """会话过期；暂停并等待人工登录。"""


class AdaptationRequired(Exception):
    """字段或页面结构未知；不允许据此推断成功。"""


class RetryableError(Exception):
    """有限次数恢复后可重试的浏览器、网络或媒体问题。"""


class Interrupted(Exception):
    """用户暂停、重新扫描或退出。"""
