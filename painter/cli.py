"""命令行入口。"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path
from typing import Iterator

from .http import HttpClient
from .runner import Crawler, preview
from .sources import MetSource, Work

log = logging.getLogger("painter")

DEFAULT_CACHE = Path(os.environ.get("PAINTER_CACHE", Path.home() / ".cache" / "painter"))
DEFAULT_INDEX = DEFAULT_CACHE / "MetObjects.csv"


def _filtered(works: Iterator[Work], *, departments: list[str] | None = None) -> Iterator[Work]:
    wanted = {name.casefold() for name in (departments or [])}
    for work in works:
        if wanted and work.department.casefold() not in wanted:
            continue
        yield work


def _run_met(args: argparse.Namespace) -> int:
    client = HttpClient(rate_per_sec=args.rate, max_attempts=args.attempts)
    source = MetSource(client, index_path=Path(args.index))

    if args.refresh_index and Path(args.index).exists():
        Path(args.index).unlink()

    if args.object_id:
        works: Iterator[Work] = (source.work_from_object_id(object_id) for object_id in args.object_id)
    else:
        works = source.iter_works()

    works = _filtered(works, departments=args.department)

    if args.dry_run:
        for uid, filename in preview(works, limit=args.limit):
            print(f"{uid}\t{filename}")
        return 0

    public_domain = True if args.public_domain_only else False if args.restricted_only else None
    crawler = Crawler(
        source,
        client,
        Path(args.out),
        overwrite=args.overwrite,
        public_domain=public_domain,
    )
    stats = crawler.crawl(works, workers=args.workers, limit=args.limit)
    log.info(
        "完成：下载 %d 张（%.1f MB），本地已有 %d 件，被过滤 %d 件，无图片 %d 件，失败 %d 件",
        stats.downloaded,
        stats.bytes / 1e6,
        stats.already,
        stats.skipped,
        stats.no_image,
        stats.failed,
    )
    for failure in stats.failures[:20]:
        log.warning("失败：%s", failure)
    return 1 if stats.failed and not stats.downloaded else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="painter", description=__doc__)
    parser.add_argument("-v", "--verbose", action="store_true", help="输出 debug 日志")
    subparsers = parser.add_subparsers(dest="source", required=True)

    met = subparsers.add_parser("met", help="大都会艺术博物馆")
    met.add_argument("--out", default="paintings/met", help="输出目录（默认：%(default)s）")
    met.add_argument("--index", default=str(DEFAULT_INDEX), help="MetObjects.csv 路径（缺失时自动下载）")
    met.add_argument("--refresh-index", action="store_true", help="重新下载 MetObjects.csv")
    met.add_argument("--limit", type=int, default=None, help="产生 N 个结果后停止（被过滤掉的作品不计数）")
    met.add_argument("--department", action="append", help="只抓这个部门（可重复指定）")
    met.add_argument("--object-id", action="append", type=int, default=None, help="按对象 ID 抓取，绕过索引（可重复指定）")
    met.add_argument("--public-domain-only", action="store_true", help="跳过仍在版权期内的作品（以 API 状态为准）")
    met.add_argument("--restricted-only", action="store_true", help="只抓仍在版权期内的作品（以 API 状态为准）")
    met.add_argument("--workers", type=int, default=4, help="并发下载数（默认：%(default)s）")
    met.add_argument("--rate", type=float, default=4.0, help="每秒最大请求数（默认：%(default)s）")
    met.add_argument("--attempts", type=int, default=5, help="每个请求的重试次数（默认：%(default)s）")
    met.add_argument("--overwrite", action="store_true", help="重新下载已记录为完成的作品")
    met.add_argument("--dry-run", action="store_true", help="只打印 UID 和文件名，不联网")
    met.set_defaults(func=_run_met)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stderr,
    )
    if getattr(args, "public_domain_only", False) and getattr(args, "restricted_only", False):
        log.error("--public-domain-only 与 --restricted-only 不能同时使用")
        return 2
    return args.func(args)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
