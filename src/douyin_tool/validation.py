"""Web 和命令行共用的输入校验，不依赖下载库或网络。"""

from datetime import date
from string import Formatter
from urllib.parse import urlparse


def nonnegative_int(value, label="下载条数", default=0):
    if value in (None, ""):
        return default
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ValueError(f"{label}必须是非负整数。")
    try:
        result = int(value)
    except ValueError as exc:
        raise ValueError(f"{label}必须是非负整数。") from exc
    if result < 0:
        raise ValueError(f"{label}不能为负数。")
    return result


def date_range(start=None, end=None):
    def parse(value):
        if value in (None, ""):
            return None
        if not isinstance(value, str):
            raise ValueError("日期必须使用 YYYY-MM-DD 格式。")
        value = value.strip()
        try:
            parsed = date.fromisoformat(value)
        except ValueError as exc:
            raise ValueError("日期必须是有效的 YYYY-MM-DD 日期。") from exc
        if parsed.isoformat() != value:
            raise ValueError("日期必须使用 YYYY-MM-DD 格式。")
        return value
    start, end = parse(start), parse(end)
    if start and end and start > end:
        raise ValueError("起始日期不能晚于结束日期。")
    return start, end


def naming_template(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError("文件名模板不能为空。")
    value = value.strip()
    if any(c in value for c in '\\/:*?"<>|\r\n'):
        raise ValueError("文件名模板不能包含路径分隔符或非法字符。")
    allowed = {"create", "nickname", "desc", "aweme_id", "uid"}
    try:
        fields = list(Formatter().parse(value))
    except ValueError as exc:
        raise ValueError("文件名模板的花括号不匹配。") from exc
    if any(field is not None and (field not in allowed or spec or conversion)
           for _, field, spec, conversion in fields):
        raise ValueError("文件名模板只能使用 create、nickname、desc、aweme_id、uid 占位符。")
    if len(value) > 150 or value in (".", ".."):
        raise ValueError("文件名模板过长或无效。")
    return value


def proxy_url(value):
    if not isinstance(value, str):
        raise ValueError("代理地址必须是文本。")
    value = value.strip()
    if not value:
        return ""
    try:
        parsed = urlparse(value)
        port = parsed.port
        if parsed.scheme not in ("http", "https", "socks5") or not parsed.hostname or port == 0:
            raise ValueError
    except ValueError as exc:
        raise ValueError("代理地址应为 http://、https:// 或 socks5:// 开头的有效地址。") from exc
    return value


def download_workers(value, default=2, maximum=8):
    """任务内并发下载条数：1..maximum，默认 2。"""
    result = nonnegative_int(value, label="并发数", default=default)
    if result < 1:
        raise ValueError("并发数至少为 1。")
    if result > maximum:
        raise ValueError(f"并发数不能超过 {maximum}。")
    return result


def max_parallel_tasks(value, default=1, maximum=4):
    """同时进行的下载任务数：1..maximum，默认 1。"""
    result = nonnegative_int(value, label="并行任务数", default=default)
    if result < 1:
        raise ValueError("并行任务数至少为 1。")
    if result > maximum:
        raise ValueError(f"并行任务数不能超过 {maximum}。")
    return result


def concurrency_mode(value, default="thread"):
    """并发模式：thread（协程/多线程）或 process（多进程，实验）。"""
    if value in (None, ""):
        return default
    if not isinstance(value, str):
        raise ValueError("并发模式必须是文本。")
    mode = value.strip().lower()
    if mode in ("thread", "async", "asyncio"):
        return "thread"
    if mode in ("process", "proc", "mp"):
        return "process"
    raise ValueError("并发模式只能是 thread 或 process。")
