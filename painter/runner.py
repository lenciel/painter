"""爬取流程：给作品取名、解析图片、下载，并维护 manifest。"""

from __future__ import annotations

import json
import logging
import threading
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Iterator
from urllib.parse import urlparse

from .http import HttpClient
from .naming import slugify, work_filename, work_stem, year_component
from .sources.base import Source, Work

log = logging.getLogger(__name__)

MANIFEST_NAME = ".manifest.jsonl"

# 线上实际会返回的 content type；Met 的两条图片链路都是 JPEG。
_EXT_BY_CONTENT_TYPE = {
    "image/jpeg": "jpeg",
    "image/jpg": "jpeg",
    "image/png": "png",
    "image/webp": "webp",
    "image/tiff": "tiff",
    "image/gif": "gif",
    "image/avif": "avif",
}
_EXT_BY_URL_SUFFIX = {
    ".jpg": "jpeg",
    ".jpeg": "jpeg",
    ".png": "png",
    ".webp": "webp",
    ".tif": "tiff",
    ".tiff": "tiff",
    ".gif": "gif",
}


@dataclass
class Record:
    source: str
    uid: str
    status: str
    filename: str = ""
    image_url: str = ""
    image_kind: str = ""
    size: int = 0
    rights: str = ""
    detail: str = ""
    updated_at: str = ""

    def key(self) -> tuple[str, str]:
        return (self.source, self.uid)


@dataclass
class CrawlStats:
    downloaded: int = 0
    already: int = 0
    skipped: int = 0
    no_image: int = 0
    failed: int = 0
    seen: int = 0
    bytes: int = 0
    failures: list[str] = field(default_factory=list)

    @property
    def processed(self) -> int:
        return self.downloaded + self.skipped + self.no_image + self.failed


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Manifest:
    """只追加的 JSONL 台账，记录爬虫处理过的每一件作品。"""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.records: dict[tuple[str, str], Record] = {}
        self._lock = threading.Lock()
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        with self.path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    payload = json.loads(line)
                    record = Record(**payload)
                except (json.JSONDecodeError, TypeError):
                    log.warning("skipping malformed manifest line")
                    continue
                self.records[record.key()] = record

    def get(self, work: Work) -> Record | None:
        return self.records.get((work.source, work.uid))

    def filenames(self) -> dict[str, tuple[str, str]]:
        return {
            record.filename: record.key()
            for record in self.records.values()
            if record.filename
        }

    def append(self, record: Record) -> None:
        record.updated_at = _now()
        with self._lock:
            self.records[record.key()] = record
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(asdict(record), ensure_ascii=False) + "\n")
                handle.flush()


class NameRegistry:
    """分配唯一文件名；撞名时给作品追加它的 object id。"""

    def __init__(self, owners: dict[str, tuple[str, str]] | None = None) -> None:
        self._owners: dict[str, tuple[str, str]] = dict(owners or {})
        self._lock = threading.Lock()

    def reserve(self, base: str, owner: tuple[str, str]) -> str:
        with self._lock:
            if self._owners.get(base) in (None, owner):
                self._owners[base] = owner
                return base
            stem, _, extension = base.rpartition(".")
            tag = slugify(owner[1], fallback="dup")
            candidate = f"{stem}_{tag}.{extension}"
            counter = 2
            while self._owners.get(candidate) not in (None, owner):
                candidate = f"{stem}_{tag}_{counter}.{extension}"
                counter += 1
            self._owners[candidate] = owner
            return candidate


def extension_for(url: str, content_type: str | None = None) -> str:
    if content_type:
        base = content_type.split(";")[0].strip().lower()
        if base in _EXT_BY_CONTENT_TYPE:
            return _EXT_BY_CONTENT_TYPE[base]
    suffix = Path(urlparse(url).path).suffix.lower()
    return _EXT_BY_URL_SUFFIX.get(suffix, "jpeg")


def filename_for(work: Work, extension: str = "jpeg") -> str:
    year = year_component(work.object_date, work.begin_date, work.end_date)
    return work_filename(work.artist, work.title, year, extension=extension)


class Crawler:
    def __init__(
        self,
        source: Source,
        client: HttpClient,
        out_dir: Path,
        *,
        overwrite: bool = False,
        public_domain: bool | None = None,
    ) -> None:
        self.source = source
        self.client = client
        self.out_dir = Path(out_dir)
        self.overwrite = overwrite
        # None keeps both; otherwise only works whose *resolved* status matches.
        self.public_domain = public_domain
        self.manifest = Manifest(self.out_dir / MANIFEST_NAME)
        self.registry = NameRegistry(self.manifest.filenames())
        self.stats = CrawlStats()
        self._stats_lock = threading.Lock()

    # ------------------------------------------------------------------ work

    def _should_skip(self, work: Work) -> bool:
        if self.overwrite:
            return False
        record = self.manifest.get(work)
        if record is None or record.status != "ok":
            return False
        return (self.out_dir / record.filename).exists()

    def _next_candidate(self, iterator: Iterator[Work]) -> Work | None:
        for work in iterator:
            self.stats.seen += 1
            if self._should_skip(work):
                self.stats.already += 1
                continue
            return work
        return None

    def _process(self, work: Work) -> Record:
        resolution = self.source.resolve(work)
        work = resolution.work
        rights = work.extra.get("rights", "")
        if self.public_domain is not None and work.is_public_domain != self.public_domain:
            wanted = "public domain" if self.public_domain else "still under copyright"
            return Record(work.source, work.uid, "skipped", rights=rights, detail=f"not {wanted}")
        reference = resolution.image
        if reference is None:
            return Record(
                work.source, work.uid, "no-image", rights=rights, detail="no usable image found"
            )

        year = year_component(work.object_date, work.begin_date, work.end_date)
        stem = work_stem(work.artist, work.title, year)
        extension = extension_for(reference.url)
        incoming = self.out_dir / f".incoming-{work.uid}"
        size, content_type = self.client.download(reference.url, incoming)

        # 以服务器真实返回的类型为准，而不是 URL 后缀。
        actual_extension = extension_for(reference.url, content_type)
        if actual_extension != extension:
            extension = actual_extension
        filename = f"{stem}.{extension}"
        filename = self.registry.reserve(filename, (work.source, work.uid))
        dest = self.out_dir / filename
        incoming.replace(dest)
        return Record(
            work.source,
            work.uid,
            "ok",
            filename=filename,
            image_url=reference.url,
            image_kind=reference.kind,
            size=size,
            rights=rights,
        )

    def _track(self, record: Record) -> None:
        with self._stats_lock:
            if record.status == "ok":
                self.stats.downloaded += 1
                self.stats.bytes += record.size
                log.info("已保存 %s（%.1f MB）", record.filename, record.size / 1e6)
            elif record.status == "no-image":
                self.stats.no_image += 1
                log.info("无图片：%s/%s（%s）", record.source, record.uid, record.detail)
            elif record.status == "skipped":
                self.stats.skipped += 1
                log.debug("已跳过 %s/%s（%s）", record.source, record.uid, record.detail)
            else:
                self.stats.failed += 1
                self.stats.failures.append(f"{record.source}/{record.uid}: {record.detail}")
                log.warning("失败 %s/%s：%s", record.source, record.uid, record.detail)

    def crawl(self, works: Iterable[Work], *, workers: int = 4, limit: int | None = None) -> CrawlStats:
        """执行下载。

        ``limit`` 约束的是「产生了结果」的作品数（已下载、无图片或失败）；
        被公有领域过滤器丢掉的作品不占额度，所以带过滤的爬取仍能拿到 ``limit`` 张图。
        """
        self.out_dir.mkdir(parents=True, exist_ok=True)
        workers = max(1, workers)
        window = workers * 4
        iterator = iter(works)
        state = {"accepted": 0, "handled": 0}

        def handle(future: Future, work: Work) -> None:
            try:
                record = future.result()
            except Exception as exc:  # noqa: BLE001 - 单件作品出错不能中断整轮爬取
                record = Record(
                    work.source,
                    work.uid,
                    "error",
                    rights=work.extra.get("rights", ""),
                    detail=f"{type(exc).__name__}: {exc}",
                )
            self.manifest.append(record)
            self._track(record)
            state["handled"] += 1
            if record.status != "skipped":
                state["accepted"] += 1
            if state["handled"] % 25 == 0:
                log.info("进度：已处理 %d 件，已保存 %d 张", state["handled"], self.stats.downloaded)

        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures: dict[Future, Work] = {}

            def top_up() -> None:
                while len(futures) < window and (
                    limit is None or state["accepted"] + len(futures) < limit
                ):
                    work = self._next_candidate(iterator)
                    if work is None:
                        return
                    futures[pool.submit(self._process, work)] = work

            top_up()
            while futures:
                done, _ = wait(list(futures), return_when=FIRST_COMPLETED)
                for future in done:
                    handle(future, futures.pop(future))
                top_up()

        log.info(
            "本轮处理 %d 件（%d 件本地已有，索引里共扫描 %d 件）",
            state["accepted"],
            self.stats.already,
            self.stats.seen,
        )
        return self.stats


def preview(works: Iterator[Work], limit: int | None = None) -> list[tuple[str, str]]:
    """试运行：不联网，只列出 (uid, 文件名)。"""
    rows: list[tuple[str, str]] = []
    for work in works:
        rows.append((work.uid, filename_for(work)))
        if limit is not None and len(rows) >= limit:
            break
    return rows
