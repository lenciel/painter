from pathlib import Path

from painter.http import HttpError
from painter.runner import NameRegistry, extension_for, filename_for
from painter.sources.base import Work
from painter.sources.met import MetSource, csv_text, page_url


class FakeClient:
    def __init__(self, html: str = "", payload: dict | None = None):
        self.html = html
        self.payload = payload or {}
        self.urls: list[str] = []

    def get_text(self, url, headers=None):
        self.urls.append(url)
        return self.html

    def get_json(self, url, headers=None):
        self.urls.append(url)
        return self.payload


def make_source(tmp_path: Path, html: str = "", payload: dict | None = None) -> MetSource:
    return MetSource(FakeClient(html, payload), index_path=tmp_path / "MetObjects.csv")


HEAD = (
    "Object ID,Is Public Domain,Artist Display Name,Title,Object Date,Object Begin Date,"
    "Object End Date,Department,Classification,Object Name,Link Resource\n"
)


def write_index(tmp_path: Path, rows: str) -> Path:
    path = tmp_path / "MetObjects.csv"
    body = HEAD + rows + ("# padding\n" if len(HEAD + rows) < 1024 else "")
    path.write_text(body + "x" * max(0, 1100 - len(body)), encoding="utf-8")
    return path


PAINTINGS = (
    '10464,True,William Merritt Chase,At the Seaside,"ca. 1892",1889,1892,The American Wing,Paintings,Painting,'
    "https://www.metmuseum.org/art/collection/search/10464\n"
    '436535,True,Vincent van Gogh,"Wheat Field with Cypresses",1889,1889,1889,European Paintings,Paintings,Painting,\n'
    "818854,False,Albert Gleizes,Kubismus,1928,1928,1928,The Libraries,,,\n"
    '11417,True,Emanuel Leutze,Washington Crossing the Delaware,1851,1851,1851,The American Wing,,Painting,\n'
    '10907,True,John Singleton Copley,Portrait of a Lady,1770,1770,1770,The American Wing,,"Painting, miniature",\n'
)


def test_iter_works_keeps_paintings_and_american_wing_records(tmp_path):
    """10464 这类记录在开放数据里没有分类，但它确实是绘画。"""
    write_index(tmp_path, PAINTINGS)
    works = list(make_source(tmp_path).iter_works())
    assert [work.uid for work in works] == ["10464", "436535", "11417"]


def test_unclassified_non_painting_is_excluded(tmp_path):
    source = make_source(tmp_path)
    assert source.is_painting({"Classification": "", "Object Name": "Painting"})
    assert not source.is_painting({"Classification": "", "Object Name": "Painting, miniature"})
    assert not source.is_painting({"Classification": "", "Object Name": ""})
    assert not source.is_painting({"Classification": "Paintings-Panels", "Object Name": "Painting"})


def test_iter_works_normalises_fields(tmp_path):
    write_index(tmp_path, PAINTINGS)
    work = next(make_source(tmp_path).iter_works())
    assert work.artist == "William Merritt Chase"
    assert work.title == "At the Seaside"
    assert work.is_public_domain is True
    assert (work.begin_date, work.end_date) == (1889, 1892)
    assert work.page_url == "https://www.metmuseum.org/art/collection/search/10464"
    assert filename_for(work) == "William_Merritt_Chase_At_the_Seaside_1892.jpeg"


def test_missing_page_url_is_derived_from_object_id(tmp_path):
    write_index(tmp_path, PAINTINGS)
    works = list(make_source(tmp_path).iter_works())
    assert works[1].page_url == "https://www.metmuseum.org/art/collection/search/436535"


def test_public_domain_work_uses_api_primary_image(tmp_path):
    source = make_source(tmp_path, payload={"primaryImage": "https://images.metmuseum.org/CRDImages/ep/original/DT1567.jpg"})
    work = Work("met", "436535", "Vincent van Gogh", "Wheat Field", "1889", 1889, 1889, True,
                "https://www.metmuseum.org/art/collection/search/436535")
    resolution = source.resolve(work)
    assert resolution.image.kind == "api-primary"
    assert resolution.image.url.endswith("DT1567.jpg")


def test_copyrighted_work_uses_og_image_from_page(tmp_path):
    html = '<meta property="og:image" content="https://collectionapi.metmuseum.org/api/collection/v1/iiif/818854/1856232/restricted">'
    source = make_source(tmp_path, html=html, payload={"primaryImage": ""})
    work = Work("met", "818854", "Albert Gleizes", "Kubismus", "1928", 1928, 1928, False,
                "https://www.metmuseum.org/art/collection/search/818854")
    resolution = source.resolve(work)
    assert resolution.image.kind == "web-restricted"
    assert resolution.image.url.endswith("/restricted")


def test_api_metadata_wins_over_lossy_csv_fields(tmp_path):
    """CSV 会用 '|' 拼接作者、还会丢标题；API 不会。"""
    source = make_source(
        tmp_path,
        payload={
            "title": "Landscape after Li Cheng",
            "artistDisplayName": "Unidentified artist",
            "objectDate": "1680",
            "objectBeginDate": 1680,
            "objectEndDate": 1680,
            "isPublicDomain": True,
            "primaryImage": "https://images.metmuseum.org/CRDImages/as/original/13_100_25.jpg",
            "objectURL": "https://www.metmuseum.org/art/collection/search/35971",
        },
    )
    work = Work(
        "met", "35971", "Wang Hui Unidentified artist", "清 佚名 Landscape after Li Cheng",
        "1680", 1680, 1680, True, "https://www.metmuseum.org/art/collection/search/35971",
    )
    resolution = source.resolve(work)
    assert resolution.work.artist == "Unidentified artist"
    assert resolution.work.title == "Landscape after Li Cheng"
    assert filename_for(resolution.work) == "Unidentified_artist_Landscape_after_Li_Cheng_1680.jpeg"


def test_missing_api_record_falls_back_to_the_website(tmp_path):
    class GoneClient(FakeClient):
        def get_json(self, url, headers=None):
            raise HttpError(url, 404)

    html = '<meta property="og:image" content="https://collectionapi.metmuseum.org/api/collection/v1/iiif/35971/13_100_25/restricted">'
    source = MetSource(GoneClient(html), index_path=tmp_path / "MetObjects.csv")
    work = Work("met", "35971", "Wang Hui", "Landscape after Li Cheng", "1680", 1680, 1680, True,
                "https://www.metmuseum.org/art/collection/search/35971")
    resolution = source.resolve(work)
    assert resolution.work.title == "Landscape after Li Cheng"
    assert resolution.image.kind == "web-restricted"


def test_csv_text_and_page_url_normalisation():
    assert csv_text("Wang Hui|Unidentified artist") == "Wang Hui Unidentified artist"
    assert csv_text("") == ""
    assert csv_text(None) == ""
    assert page_url("http://www.metmuseum.org/art/collection/search/35155", 35155).startswith("https://")
    assert page_url("", 35155) == "https://www.metmuseum.org/art/collection/search/35155"


def test_og_image_accepts_reversed_attribute_order(tmp_path):
    html = '<meta content="https://images.metmuseum.org/CRDImages/ep/original/DT1567.jpg" property="og:image">'
    source = make_source(tmp_path, html=html)
    assert source.og_image("https://example.test") == "https://images.metmuseum.org/CRDImages/ep/original/DT1567.jpg"


def test_og_image_rejects_site_wide_placeholder(tmp_path):
    html = '<meta property="og:image" content="https://www.metmuseum.org/-/media/images/social-share.jpg">'
    source = make_source(tmp_path)
    assert source.og_image("https://example.test") is None


def test_og_image_rejects_image_of_another_object(tmp_path):
    html = '<meta property="og:image" content="https://collectionapi.metmuseum.org/api/collection/v1/iiif/999/1/restricted">'
    source = make_source(tmp_path)
    assert source.og_image("https://example.test", expect_object_id="818854") is None


def test_extension_detection():
    assert extension_for("https://x/y/DT95.jpg") == "jpeg"
    assert extension_for("https://x/y/image", "image/png; charset=binary") == "png"
    assert extension_for("https://x/y/image") == "jpeg"


def test_name_registry_disambiguates_collisions():
    registry = NameRegistry()
    first = registry.reserve("A_B_1892.jpeg", ("met", "1"))
    second = registry.reserve("A_B_1892.jpeg", ("met", "2"))
    assert first == "A_B_1892.jpeg"
    assert second == "A_B_1892_2.jpeg"
    # 对同一个 owner 是幂等的。
    assert registry.reserve("A_B_1892.jpeg", ("met", "1")) == "A_B_1892.jpeg"
