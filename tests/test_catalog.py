import subprocess
import sys

import pytest

from lr_mcp import catalog, server
from lr_mcp.client import LrError


def _asset(aid, captured, subtype="image", **payload):
    return {"id": aid, "subtype": subtype, "payload": {"captureDate": captured, **payload}}


ENTRIES = [  # album-asset resources in API page order
    {"asset": _asset("b", "2024-05-02T00:00:00"), "payload": {"order": "V--B"}},
    {"asset": _asset("nokey", "2024-05-05T00:00:00"), "payload": {}},
    {"asset": _asset("clip", "2024-05-04T00:00:00", subtype="video"), "payload": {"order": "V--C"}},
    {"asset": _asset("a", "2024-05-03T00:00:00"), "payload": {"order": "V--A"}},
    {"asset": _asset("int", "2024-05-01T00:00:00"), "payload": {"order": 7}},
]


@pytest.fixture
def album(monkeypatch):
    """Album `x` with ENTRIES; set album.payload to change its payload. Records GET and page requests."""
    state = type("Album", (), {})()
    state.payload = {"name": "Trip", "parent": {"id": "f"}, "assetSortOrder": "customAsc"}
    state.gets, state.pages = [], []
    catalog._album_cache.clear()

    def get_json(path, auth=True, deadline=None):
        state.gets.append(path)
        return {"id": "x", "subtype": "collection", "payload": state.payload}

    def paginate(path):
        state.pages.append(path)
        return iter(ENTRIES)

    monkeypatch.setattr(catalog.lr, "catalog_path", lambda: "/v2/catalogs/c/")
    monkeypatch.setattr(catalog.lr, "get_json", get_json)
    monkeypatch.setattr(catalog.lr, "paginate", paginate)
    return state


def _missing(monkeypatch, status):
    def get_json(path, auth=True, deadline=None):
        e = LrError(f"GET {path} returned {status}: {{'code': 1002, 'description': 'Resource not found'}}")
        e.status = status
        raise e

    monkeypatch.setattr(catalog.lr, "catalog_path", lambda: "/v2/catalogs/c/")
    monkeypatch.setattr(catalog.lr, "get_json", get_json)
    monkeypatch.setattr(catalog.lr, "paginate", lambda path: pytest.fail("no asset request for a missing album"))
    catalog._album_cache.clear()


def test_album_info(album):
    assert catalog.album_info("x") == {"id": "x", "name": "Trip", "parent": "f", "subtype": "collection",
                                       "sort": "customAsc"}
    assert album.gets == ["/v2/catalogs/c/albums/x"]


@pytest.mark.parametrize("payload, sort", [({}, "captureDateAsc"), ({"assetSortOrder": None}, "captureDateAsc"),
                                           ({"assetSortOrder": "fileNameAsc"}, "fileNameAsc")])
def test_album_info_sort_default_and_unknown(album, payload, sort):
    album.payload = payload
    info = catalog.album_info("x")
    assert info["sort"] == sort and info["parent"] is None and info["name"] is None


def test_album_info_is_cached_for_a_minute(album, monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(catalog.time, "time", lambda: now[0])
    catalog.album_info("x")
    now[0] += catalog.ALBUM_TTL
    album.payload = {"assetSortOrder": "captureDateDesc"}
    assert catalog.album_info("x")["sort"] == "customAsc"
    now[0] += 1
    assert catalog.album_info("x")["sort"] == "captureDateDesc"
    assert len(album.gets) == 2


def test_album_info_returns_a_copy(album):
    catalog.album_info("x")["sort"] = "changed"
    assert catalog.album_info("x")["sort"] == "customAsc"


def test_album_not_found(monkeypatch):
    _missing(monkeypatch, 404)
    with pytest.raises(LrError) as e:
        catalog.album_info("gone")
    assert str(e.value) == "Album not found: gone. Find album ids with lr_list_albums."
    with pytest.raises(LrError, match="^Album not found: gone"):
        catalog.album_assets("gone")


def test_album_other_errors_pass_through(monkeypatch):
    _missing(monkeypatch, 403)
    with pytest.raises(LrError, match="returned 403"):
        catalog.album_info("x")


def test_list_photos_reports_a_missing_album(monkeypatch):
    _missing(monkeypatch, 404)
    server._albums_cache = (0.0, None)
    with pytest.raises(LrError, match="^Album not found: gone"):
        server.lr_list_photos(album_id="gone")


def _ids(entries):
    return [e["asset"]["id"] for e in entries]


def test_album_assets_custom_order(album):
    out = catalog.album_assets("x")
    assert _ids(out) == ["int", "a", "b", "nokey"]  # "7" < "V--A"; no key last; video left out
    assert [e["order"] for e in out] == ["7", "V--A", "V--B", None]
    assert out[0]["asset"] is ENTRIES[4]["asset"]
    assert album.pages == ["/v2/catalogs/c/albums/x/assets?embed=asset&limit=500"]


def test_album_assets_with_videos(album):
    assert _ids(catalog.album_assets("x", images_only=False)) == ["int", "a", "b", "clip", "nokey"]


@pytest.mark.parametrize("sort, ids", [
    ("captureDateAsc", ["int", "b", "a", "nokey"]),
    (None, ["int", "b", "a", "nokey"]),
    ("captureDateDesc", ["nokey", "a", "b", "int"]),
    ("fileNameAsc", ["int", "b", "a", "nokey"]),  # unsupported: capture date ascending
])
def test_album_assets_capture_order(album, sort, ids):
    album.payload = {"assetSortOrder": sort}
    assert _ids(catalog.album_assets("x")) == ids


def test_album_assets_ties_keep_capture_order(album, monkeypatch):
    entries = [{"asset": _asset("late", "2024-05-02T00:00:00"), "payload": {"order": "V"}},
               {"asset": _asset("early", "2024-05-01T00:00:00"), "payload": {"order": "V"}}]
    monkeypatch.setattr(catalog.lr, "paginate", lambda path: iter(entries))
    assert _ids(catalog.album_assets("x")) == ["early", "late"]


def _full():
    return _asset(
        "a1", "2024-05-01T10:00:00",
        importSource={"fileName": "DSC1.ARW", "originalWidth": 6000, "originalHeight": 4000},
        xmp={"exif": {"ISOSpeedRatings": 200, "FNumber": [28, 10], "ExposureTime": [1, 250], "FocalLength": [35, 1]},
             "tiff": {"Make": "SONY", "Model": "ILCE-7M4"}, "aux": {"Lens": "FE 35mm F1.4 GM"},
             "dc": {"title": {"x-default": "Busan"}, "description": "Harbour\nat dusk",
                    "subject": {"sea": True, "night": True, "old": False}}},
        develop={"croppedWidth": 5400, "croppedHeight": 3037.6, "userOrientation": 8,
                 "userUpdated": "2024-06-01T00:00:00Z"},
        location={"latitude": 35.1, "longitude": 129.04},
        ratings={"u1": {"rating": 1, "date": "2020-01-01"}, "u2": {"rating": 4, "date": "2024-01-01"}},
        reviews={"u1": {"flag": "pick", "date": "2024-01-01"}},
    )


def test_asset_summary_is_data():
    assert catalog.asset_summary(_full()) == {
        "id": "a1", "subtype": "image", "file": "DSC1.ARW", "captured": "2024-05-01T10:00:00",
        "rating": 4, "flag": "pick", "camera": "SONY ILCE-7M4", "lens": "FE 35mm F1.4 GM", "iso": 200,
        "fnumber": 2.8, "shutter": "1/250", "exposure_time": 0.004, "focal_length": 35.0,
        "original": (6000, 4000), "cropped": (5400, 3037.6), "orientation": 8, "gps": (35.1, 129.04),
        "title": "Busan", "caption": "Harbour at dusk", "keywords": ["sea", "night"],
        "edited": "2024-06-01T00:00:00Z",
    }


def test_asset_summary_of_a_bare_asset():
    s = catalog.asset_summary({"id": "v", "payload": {"xmp": None, "develop": None, "location": None}})
    assert s["id"] == "v" and s["keywords"] == []
    assert all(v is None for k, v in s.items() if k not in ("id", "keywords"))


@pytest.mark.parametrize("subject, keywords", [
    (["sea", " night "], ["sea", "night"]),
    ({"sea": True, "old": False}, ["sea"]),
    ("sea", ["sea"]),
    (None, []),
    (42, []),
])
def test_asset_summary_keywords(subject, keywords):
    a = _asset("a", "2024-05-01T00:00:00", xmp={"dc": {"subject": subject}})
    assert catalog.asset_summary(a)["keywords"] == keywords


def test_asset_summary_long_exposure_and_odd_numbers():
    a = _asset("a", "2024-05-01T00:00:00", xmp={"exif": {"ExposureTime": [30, 1], "FNumber": float("nan"),
                                                         "FocalLength": [1, 0]}},
               develop={"croppedWidth": float("inf"), "croppedHeight": 10})
    s = catalog.asset_summary(a)
    assert (s["shutter"], s["exposure_time"]) == ("30", 30.0)
    assert s["fnumber"] is None and s["focal_length"] is None and s["cropped"] is None


def test_asset_edited():
    assert catalog.asset_edited(_full()) == "2024-06-01T00:00:00Z"
    assert catalog.asset_edited({"payload": {"develop": None, "userUpdated": "u", "changedAtTime": "c"}}) == "u"
    assert catalog.asset_edited({"payload": {"changedAtTime": "c"}}) == "c"
    assert catalog.asset_edited({"payload": None}) is None


def test_tools_share_the_album_cache(album):
    server._albums_cache = (0.0, None)
    catalog.album_info("x")
    assert server._album_sort_order("x") == "customAsc"
    assert len(album.gets) == 1


def test_catalog_imports_without_the_server():
    code = "import sys, lr_mcp.catalog; assert 'lr_mcp.server' not in sys.modules"
    subprocess.run([sys.executable, "-c", code], check=True)
