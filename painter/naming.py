"""文件名规则：``作者_标题_年代.jpeg``，空格替换为下划线。"""

from __future__ import annotations

import re
import unicodedata

# 在 macOS/Linux/Windows 的路径分量中非法或有风险字符。
_ILLEGAL = set('/\\:*?"<>|\0')
_WHITESPACE = re.compile(r"\s+")
_YEAR = re.compile(r"\d{3,4}")
_RANGE_MARKER = re.compile(r"[-–—~/]|\bto\b", re.IGNORECASE)

# 让整个文件名保持在 ext4/APFS 的 255 字节上限以内，且留有余量。
_MAX_STEM_BYTES = 200
_UNKNOWN = "unknown"


def _truncate_bytes(text: str, limit: int) -> str:
    encoded = text.encode("utf-8")
    if len(encoded) <= limit:
        return text
    return encoded[:limit].decode("utf-8", "ignore")


def slugify(value: str | None, *, fallback: str = _UNKNOWN) -> str:
    """规范化文件名中的一个组成部分。

    连续空白压缩成一个下划线（即要求的规则）；无法出现在路径分量中的字符直接丢弃；
    结果会去掉首尾的点和下划线。
    """
    text = unicodedata.normalize("NFC", (value or "").strip())
    text = "".join(ch if ch.isprintable() else " " for ch in text)
    text = "".join("" if ch in _ILLEGAL else ch for ch in text)
    text = _WHITESPACE.sub("_", text)
    text = text.strip("._")
    return text or fallback


def year_component(object_date: str | None, begin_date: int | None, end_date: int | None) -> str:
    """推导文件名里的年代部分。

    馆方自由文本日期中若只有一个明确的年份，则以它为准（"ca. 1892" -> "1892"，
    "1928" -> "1928"）。跨年则退回机器可读的起止年（"1865-1867"），
    再不然直接使用自由文本。
    """
    text = (object_date or "").strip()
    years = sorted({int(match) for match in _YEAR.findall(text)})
    if len(years) == 1 and not _RANGE_MARKER.search(text):
        return str(years[0])

    positive = [year for year in (begin_date, end_date) if year is not None and year > 0]
    if len(positive) == 2:
        return str(positive[0]) if positive[0] == positive[1] else f"{positive[0]}-{positive[1]}"
    if len(years) >= 2:
        return f"{years[0]}-{years[-1]}"
    if years:
        return str(years[0])
    return slugify(text)


def work_stem(artist: str | None, title: str | None, year: str) -> str:
    """``作者_标题_年代``，不含扩展名。"""
    stem = "_".join(part for part in (slugify(artist), slugify(title), slugify(year)) if part)
    return _truncate_bytes(stem, _MAX_STEM_BYTES).rstrip("._")


def work_filename(
    artist: str | None,
    title: str | None,
    year: str,
    *,
    extension: str = "jpeg",
) -> str:
    """生成 ``作者_标题_年代.jpeg``。"""
    extension = extension.lstrip(".")
    stem = work_stem(artist, title, year)
    return f"{stem}.{extension}" if extension else stem
