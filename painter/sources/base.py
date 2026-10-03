"""所有博物馆爬虫共用的数据源契约。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterator, Protocol


@dataclass(frozen=True)
class Work:
    """一件待采集的作品，已按统一口径归一化。"""

    source: str
    uid: str
    artist: str
    title: str
    object_date: str
    begin_date: int | None
    end_date: int | None
    is_public_domain: bool
    page_url: str
    department: str = ""
    extra: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ImageRef:
    """一个可下载的图片，以及它是怎么被找到的（写进 manifest 备查）。"""

    url: str
    kind: str


@dataclass(frozen=True)
class Resolution:
    """权威的作品元数据，以及它的图片（如果有）。"""

    work: Work
    image: ImageRef | None


class Source(Protocol):
    """一个可以被枚举并抓取图片的博物馆。"""

    slug: str

    def iter_works(self) -> Iterator[Work]:
        """产出爬虫需要考虑的每一件作品。"""

    def resolve(self, work: Work) -> Resolution:
        """返回权威元数据和最佳可下载图片。"""
