"""大都会艺术博物馆（The Met）。

枚举使用 CC0 的开放数据 CSV（``MetObjects.csv``）：因为 Collection API 的
``classification`` 字段对相当一部分记录（例如整个美国馆 The American Wing）是空的，
所以 API 无法用来做「Classification: Paintings」这个筛选。逐件作品的命名则优先采用
API 记录——对多作者（multi-constituent）作品它比 CSV 干净得多。API 同时提供公有领域
作品的图片地址；仍在版权期内的作品由对象页面的 ``og:image`` 兜底（这类记录的
``primaryImage`` 是空的）。
"""

from __future__ import annotations

import csv
import logging
import re
from dataclasses import replace
from pathlib import Path
from typing import Iterator

from ..http import HttpClient, HttpError
from .base import ImageRef, Resolution, Work

log = logging.getLogger(__name__)

SOURCE = "met"
API_ROOT = "https://collectionapi.metmuseum.org/public/collection/v1"
OBJECT_PAGE = "https://www.metmuseum.org/art/collection/search/{object_id}"
CSV_URL = "https://media.githubusercontent.com/media/metmuseum/openaccess/master/MetObjects.csv"

# API 与网站都不认 git 仓库里那份 Git LFS 指针文件。
_LFS_MARKER = b"version https://git-lfs"

_OG_IMAGE = re.compile(
    r"""<meta[^>]+property=["']og:image["'][^>]+content=["']([^"']+)["']""",
    re.IGNORECASE,
)
_OG_IMAGE_REVERSED = re.compile(
    r"""<meta[^>]+content=["']([^"']+)["'][^>]+property=["']og:image["']""",
    re.IGNORECASE,
)
_IIIF_OBJECT = re.compile(r"/iiif/(\d+)/")


def _as_int(value: str | None) -> int | None:
    if value is None:
        return None
    value = value.strip()
    if not value:
        return None
    try:
        return int(float(value))
    except ValueError:
        return None


def parse_bool(value: str | None) -> bool:
    return (value or "").strip().lower() == "true"


def csv_text(value: str | None) -> str:
    """CSV 会用 ``|`` 连接多个作者/角色；这里把它换成空格。"""
    return re.sub(r"\s+", " ", (value or "").replace("|", " ")).strip()


def page_url(value: str | None, object_id: int) -> str:
    url = (value or "").strip()
    if url.startswith("http://www.metmuseum.org"):
        url = "https://www.metmuseum.org" + url[len("http://www.metmuseum.org") :]
    return url or OBJECT_PAGE.format(object_id=object_id)


class MetSource:
    slug = SOURCE

    def __init__(
        self,
        client: HttpClient,
        *,
        index_path: Path,
        classification: str = "Paintings",
        object_name_fallback: str | None = "Painting",
    ) -> None:
        self.client = client
        self.index_path = Path(index_path)
        self.classification = classification
        # 馆方开放数据把美国馆（The American Wing）的所有画作 `Classification`
        # 都留空（例如对象 10464《At the Seaside》），但仍把它们标注为
        # Object Name "Painting"；这些记录同样要收进来。
        self.object_name_fallback = object_name_fallback

    # ------------------------------------------------------------------ index

    def ensure_index(self) -> Path:
        """缺少索引时下载 MetObjects.csv，并拒绝 Git LFS 指针文件。"""
        path = self.index_path
        if path.exists() and path.stat().st_size > 1024:
            with path.open("rb") as handle:
                if not handle.read(len(_LFS_MARKER)).startswith(_LFS_MARKER):
                    return path
            log.warning("%s 是 Git LFS 指针文件，重新下载", path)
        path.parent.mkdir(parents=True, exist_ok=True)
        log.info("下载 %s -> %s（约 320 MB）", CSV_URL, path)
        self.client.download(CSV_URL, path)
        return path

    def is_painting(self, row: dict) -> bool:
        classification = (row.get("Classification") or "").strip()
        if classification == self.classification:
            return True
        if classification:
            return False
        return bool(self.object_name_fallback) and (
            (row.get("Object Name") or "").strip() == self.object_name_fallback
        )

    def iter_works(self) -> Iterator[Work]:
        """产出开放数据中属于绘画的每一件作品。"""
        path = self.ensure_index()
        log.info(
            "扫描 %s，条件为 Classification=%r（未分类时用 Object Name=%r）",
            path,
            self.classification,
            self.object_name_fallback,
        )
        matched = 0
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                if not self.is_painting(row):
                    continue
                object_id = _as_int(row.get("Object ID"))
                if object_id is None:
                    continue
                matched += 1
                yield Work(
                    source=SOURCE,
                    uid=str(object_id),
                    artist=csv_text(row.get("Artist Display Name")),
                    title=csv_text(row.get("Title")),
                    object_date=csv_text(row.get("Object Date")),
                    begin_date=_as_int(row.get("Object Begin Date")),
                    end_date=_as_int(row.get("Object End Date")),
                    is_public_domain=parse_bool(row.get("Is Public Domain")),
                    page_url=page_url(row.get("Link Resource"), object_id),
                    department=csv_text(row.get("Department")),
                    extra={
                        "object_name": csv_text(row.get("Object Name")),
                        "rights": csv_text(row.get("Rights and Reproduction")),
                    },
                )
        log.info("共找到 %d 件符合绘画条件的作品", matched)

    def work_from_object_id(self, object_id: int) -> Work:
        """直接按 API 记录构造 Work（``--object-id`` 用）。"""
        record = self.object(object_id)
        return Work(
            source=SOURCE,
            uid=str(object_id),
            artist=(record.get("artistDisplayName") or "").strip(),
            title=(record.get("title") or "").strip(),
            object_date=(record.get("objectDate") or "").strip(),
            begin_date=record.get("objectBeginDate"),
            end_date=record.get("objectEndDate"),
            is_public_domain=bool(record.get("isPublicDomain")),
            page_url=record.get("objectURL") or OBJECT_PAGE.format(object_id=object_id),
            department=(record.get("department") or "").strip(),
            extra={
                "classification": (record.get("classification") or "").strip(),
                "rights": (record.get("rightsAndReproduction") or "").strip(),
            },
        )

    # ----------------------------------------------------------------- images

    def object(self, object_id: int | str) -> dict:
        return self.client.get_json(f"{API_ROOT}/objects/{object_id}")

    def resolve(self, work: Work) -> Resolution:
        """先取 API 记录，再取图片。

        CSV 对多作者作品是有损的（用一个 ``|`` 把名字拼在一起，部分标题还是空的），
        所以命名始终优先 API 的 ``artistDisplayName``/``title``/``objectDate``，
        API 记录缺失时才退回 CSV。
        """
        record: dict = {}
        try:
            record = self.object(work.uid)
        except HttpError as exc:
            if exc.status != 404:
                raise
            log.debug("对象 %s 不在 API 中，只使用索引元数据", work.uid)
        enriched = self._merge(work, record)

        primary = (record.get("primaryImage") or "").strip()
        if primary:
            return Resolution(enriched, ImageRef(primary, "api-primary"))

        # 仍在版权期内的作品在 API 里没有 primaryImage；对象页面通过其
        # Open Graph 元数据暴露一张 "restricted" 的 IIIF JPEG。
        page_image = self.og_image(enriched.page_url, expect_object_id=work.uid)
        if page_image:
            return Resolution(enriched, ImageRef(page_image, "web-restricted"))
        return Resolution(enriched, None)

    @staticmethod
    def _merge(work: Work, record: dict) -> Work:
        if not record:
            return work
        begin = record.get("objectBeginDate")
        end = record.get("objectEndDate")
        extra = dict(work.extra)
        rights = (record.get("rightsAndReproduction") or "").strip()
        if rights:
            extra["rights"] = rights
        return replace(
            work,
            artist=(record.get("artistDisplayName") or "").strip() or work.artist,
            title=(record.get("title") or "").strip() or work.title,
            object_date=(record.get("objectDate") or "").strip() or work.object_date,
            begin_date=begin if isinstance(begin, int) else work.begin_date,
            end_date=end if isinstance(end, int) else work.end_date,
            is_public_domain=bool(record.get("isPublicDomain", work.is_public_domain)),
            page_url=(record.get("objectURL") or "").strip() or work.page_url,
            department=(record.get("department") or "").strip() or work.department,
            extra=extra,
        )

    def og_image(self, page_url: str, *, expect_object_id: str | None = None) -> str | None:
        html = self.client.get_text(page_url)
        match = _OG_IMAGE.search(html) or _OG_IMAGE_REVERSED.search(html)
        if not match:
            return None
        url = match.group(1).strip()
        if not url.startswith(("https://collectionapi.metmuseum.org/", "https://images.metmuseum.org/")):
            # 全站通用的分享图，不是这件作品的图。
            return None
        if expect_object_id:
            found = _IIIF_OBJECT.search(url)
            if found and found.group(1) != str(expect_object_id):
                return None
        return url
