import json
from pathlib import Path

from painter.runner import Crawler
from painter.sources.base import ImageRef, Resolution, Work


class FakeSource:
    slug = "fake"

    def __init__(self, works):
        self.works = works

    def iter_works(self):
        return iter(self.works)

    def resolve(self, work):
        if work.uid == "3":
            return Resolution(work, None)
        return Resolution(work, ImageRef(f"https://example.test/{work.uid}.jpg", "test"))


class FakeDownloader:
    def __init__(self):
        self.urls = []

    def download(self, url, dest: Path):
        self.urls.append(url)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"\xff\xd8\xff" + b"0" * 97)
        return 100, "image/jpeg"


def work(uid, title, year="1892", pd=True):
    return Work(
        source="fake",
        uid=uid,
        artist="Same Artist",
        title=title,
        object_date=year,
        begin_date=int(year),
        end_date=int(year),
        is_public_domain=pd,
        page_url=f"https://example.test/{uid}",
        extra={"rights": "CC0"},
    )


def test_crawl_writes_named_files_and_manifest(tmp_path):
    source = FakeSource([work("1", "Twin Work"), work("2", "Twin Work"), work("3", "Missing")])
    crawler = Crawler(source, FakeDownloader(), tmp_path)
    stats = crawler.crawl(source.iter_works())

    assert stats.downloaded == 2
    assert stats.no_image == 1
    assert (tmp_path / "Same_Artist_Twin_Work_1892.jpeg").exists()
    assert (tmp_path / "Same_Artist_Twin_Work_1892_2.jpeg").exists()

    records = [json.loads(line) for line in (tmp_path / ".manifest.jsonl").read_text().splitlines()]
    assert {r["status"] for r in records} == {"ok", "no-image"}
    ok = next(r for r in records if r["status"] == "ok")
    assert ok["rights"] == "CC0"
    assert ok["image_kind"] == "test"


def test_crawl_is_resumable(tmp_path):
    source = FakeSource([work("1", "Twin Work"), work("3", "Missing")])
    downloader = FakeDownloader()
    crawler = Crawler(source, downloader, tmp_path)
    crawler.crawl(source.iter_works())
    assert len(downloader.urls) == 1

    second = Crawler(source, downloader, tmp_path)
    stats = second.crawl(source.iter_works())
    assert stats.already == 1
    assert stats.downloaded == 0
    assert len(downloader.urls) == 1  # 已完成的作品不会再发一次请求


def test_overwrite_refetches(tmp_path):
    source = FakeSource([work("1", "Twin Work")])
    downloader = FakeDownloader()
    Crawler(source, downloader, tmp_path).crawl(source.iter_works())
    Crawler(source, downloader, tmp_path, overwrite=True).crawl(source.iter_works())
    assert len(downloader.urls) == 2


def test_public_domain_filter_applies_after_resolution_and_does_not_use_up_the_limit(tmp_path):
    source = FakeSource([work("1", "A", pd=False), work("4", "B", pd=True), work("5", "C", pd=True)])
    crawler = Crawler(source, FakeDownloader(), tmp_path, public_domain=True)
    stats = crawler.crawl(source.iter_works(), limit=2)
    assert stats.downloaded == 2
    assert stats.skipped == 1
    assert not list(tmp_path.glob("Same_Artist_A_*"))
