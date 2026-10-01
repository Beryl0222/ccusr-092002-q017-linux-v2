"""时间工具：统一使用带时区的 ISO-8601，历史重放不依赖当前时钟。"""

from datetime import datetime, timezone
from typing import Optional

MISSING = object()


def now() -> datetime:
    """当前 UTC 时间（只在写入新事件时使用）。"""
    return datetime.now(timezone.utc)


def now_iso() -> str:
    return to_iso(now())


def to_iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        raise ValueError("时间必须带时区")
    return dt.isoformat()


def parse(value) -> datetime:
    """解析 ISO-8601；拒绝 naive 时间，避免歧义。"""
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str):
        dt = datetime.fromisoformat(value)
    else:
        raise ValueError(f"无法解析时间: {value!r}")
    if dt.tzinfo is None:
        raise ValueError("时间必须带时区，禁止使用 naive datetime")
    return dt


def optional_parse(value) -> Optional[datetime]:
    if value is None:
        return None
    return parse(value)


def day_key(dt: datetime) -> str:
    """按事件自身时区归入自然日。"""
    return dt.date().isoformat()


def day_end(day: str) -> datetime:
    """某自然日结束时刻（UTC 仅用于 as_of 比较边界）。"""
    d = datetime.fromisoformat(day)
    return d.replace(hour=23, minute=59, second=59, microsecond=999999, tzinfo=timezone.utc)
