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
        "title": "Busan", "caption": "Harbour\nat dusk", "keywords": ["sea", "night"],
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


def test_album_not_found_keeps_the_status(monkeypatch):
    _missing(monkeypatch, 404)
    with pytest.raises(LrError) as e:
        catalog.album_info("gone")
    assert e.value.status == 404


def test_album_info_types_on_malformed_album(album, monkeypatch):
    monkeypatch.setattr(catalog.lr, "get_json", lambda path, auth=True, deadline=None: {
        "id": 5, "subtype": ["collection"], "payload": {"name": 3, "parent": "f", "assetSortOrder": {"x": 1}}})
    assert catalog.album_info("x") == {"id": "x", "name": None, "parent": None, "subtype": None,
                                       "sort": "captureDateAsc"}
    catalog._album_cache.clear()
    monkeypatch.setattr(catalog.lr, "get_json", lambda path, auth=True, deadline=None: {"id": "x", "payload": [1]})
    assert catalog.album_info("x")["sort"] == "captureDateAsc"


def _exif(**exif):
    return _asset("a", "2024-05-01T00:00:00", xmp={"exif": exif})


@pytest.mark.parametrize("value, shutter, seconds", [
    (0.5, "0.5", 0.5),
    (2, "2", 2.0),
    (30.0, "30.0", 30.0),
    ([1, 250], "1/250", 0.004),
    ("1/60", "1/60", None),        # text is kept as text, but it is no number
    ([1, 0], None, None),
    ([True, 2], None, None),
    (True, None, None),
    ({"n": 1}, None, None),
    (float("nan"), None, None),
])
def test_asset_summary_shutter_is_text(value, shutter, seconds):
    s = catalog.asset_summary(_exif(ExposureTime=value))
    assert s["shutter"] == shutter and s["exposure_time"] == seconds
    assert s["shutter"] is None or isinstance(s["shutter"], str)
    assert s["exposure_time"] is None or type(s["exposure_time"]) is float


def test_numeric_shutter_keeps_the_tool_line():
    a = _exif(ExposureTime=0.5, FNumber=8, FocalLength=35)
    assert server._fmt(server._summary(a)) == "a | ? | 2024-05-01T00:00:00 | f/8 0.5s 35mm"


def test_asset_summary_floats_from_ints():
    s = catalog.asset_summary(_exif(FNumber=8, FocalLength=35, ExposureTime=1))
    assert (s["fnumber"], s["focal_length"], s["exposure_time"]) == (8.0, 35.0, 1.0)
    assert all(type(s[k]) is float for k in ("fnumber", "focal_length", "exposure_time"))
    s = catalog.asset_summary(_exif(FNumber=[28, 10], FocalLength=[50, 1]))
    assert type(s["fnumber"]) is float and type(s["focal_length"]) is float
    s = catalog.asset_summary(_exif(FNumber=True, FocalLength="35", ExposureTime=[1, False]))
    assert s["fnumber"] is None and s["focal_length"] is None and s["exposure_time"] is None


@pytest.mark.parametrize("width, height", [(6000, None), (6000, "4000"), (True, 4000), (6000, float("nan")),
                                           (0, 4000), (None, 4000)])
def test_asset_summary_original_needs_two_numbers(width, height):
    a = _asset("a", "2024-05-01T00:00:00", importSource={"originalWidth": width, "originalHeight": height})
    assert catalog.asset_summary(a)["original"] is None
    assert server._summary(a)["size"] is None  # the tool printed e.g. "size: 6000xNone" before


def test_asset_summary_types_on_malformed_asset():
    a = {"id": "a", "subtype": 1, "payload": {
        "captureDate": 20240501, "importSource": {"fileName": ["x.jpg"]},
        "xmp": {"exif": {"ISOSpeedRatings": "high"}, "tiff": {"Make": 1, "Model": "M"}, "aux": {"Lens": {"x": 1}},
                "dc": {"title": 5, "subject": [1, "sea"]}},
        "ratings": {"u": {"rating": True, "date": None}, "v": "junk"},
        "reviews": {"u": {"flag": 1, "date": "2024"}},
        "develop": {"userOrientation": True, "userUpdated": 17},
        "location": {"latitude": "35", "longitude": 129},
        "userUpdated": "", "changedAtTime": "2024-07-01T00:00:00Z",
    }}
    s = catalog.asset_summary(a)
    assert s == {**{k: None for k in s}, "id": "a", "camera": "M", "keywords": ["sea"],
                 "edited": "2024-07-01T00:00:00Z"}


def test_asset_summary_sections_of_the_wrong_type():
    a = {"id": "a", "payload": {"xmp": {"exif": [1], "tiff": "x", "dc": 3, "aux": []}, "develop": [],
                                "location": "x", "importSource": [], "ratings": [5], "reviews": "pick"}}
    s = catalog.asset_summary(a)
    assert s["keywords"] == [] and all(v is None for k, v in s.items() if k not in ("id", "keywords"))
    assert catalog.asset_summary({"id": "b", "payload": "x"})["captured"] is None


@pytest.mark.parametrize("iso, expected", [(400, 400), ([800], 800), ([], None), ("200", 200), (["200"], 200), ("ISO 100", None), ("²", None),
                                         (True, None)])
def test_asset_summary_iso(iso, expected):
    assert catalog.asset_summary(_exif(ISOSpeedRatings=iso))["iso"] == expected


def test_asset_summary_rating_is_an_int():
    a = _asset("a", "2024-05-01T00:00:00", ratings={"u": {"rating": 4.5, "date": "2024"}})
    assert catalog.asset_summary(a)["rating"] is None
    a["payload"]["ratings"]["v"] = {"rating": 3, "date": "2025"}
    assert catalog.asset_summary(a)["rating"] == 3


def test_asset_edited_skips_values_that_are_not_text():
    assert catalog.asset_edited({"payload": {"develop": {"userUpdated": 5}, "userUpdated": "u"}}) == "u"
    assert catalog.asset_edited({"payload": {"develop": "x", "userUpdated": {"t": 1}}}) is None


def test_album_order_tolerates_odd_capture_dates(album, monkeypatch):
    entries = [{"asset": _asset("num", 20240501), "payload": {}},
               {"asset": _asset("str", "2024-05-01T00:00:00"), "payload": {}}]
    monkeypatch.setattr(catalog.lr, "paginate", lambda path: iter(entries))
    album.payload = {"assetSortOrder": "captureDateAsc"}
    assert _ids(catalog.album_assets("x")) == ["num", "str"]


@pytest.mark.parametrize("exposure, line", [  # main printed str(value) + "s" for a numeric ExposureTime
    (30.0, "30.0s"),
    (0.0001234567, "0.0001234567s"),
    (1 / 3, "0.3333333333333333s"),
    (0.5, "0.5s"),
    (2, "2s"),
])
def test_numeric_exposure_time_tool_line_as_on_main(exposure, line):
    a = _exif(ExposureTime=exposure)
    assert server._fmt(server._summary(a)) == f"a | ? | 2024-05-01T00:00:00 | {line}"


@pytest.mark.parametrize("resource", [{}, {"id": 5}, {"id": None, "payload": {}}, None, "a1"])
def test_asset_summary_needs_a_string_id(resource):
    with pytest.raises(LrError, match="no string id"):
        catalog.asset_summary(resource)


@pytest.mark.parametrize("rating, expected", [(0, 0), (5, 5), (6, None), (-1, None), (4.0, None), (True, None)])
def test_asset_summary_rating_range(rating, expected):
    a = _asset("a", "2024-05-01T00:00:00", ratings={"u": {"rating": rating, "date": "2024"}})
    assert catalog.asset_summary(a)["rating"] == expected


@pytest.mark.parametrize("w, h", [(0, 4000), (6000, 0), (-6000, 4000), (6000, -1.5), (0.0, 0.0)])
def test_sizes_must_be_positive(w, h):
    a = _asset("a", "2024-05-01T00:00:00", importSource={"originalWidth": w, "originalHeight": h},
               develop={"croppedWidth": w, "croppedHeight": h})
    s = catalog.asset_summary(a)
    assert s["original"] is None and s["cropped"] is None
    assert server._summary(a)["size"] is None and server._summary(a)["cropped"] is None


CAPTION = "  첫 줄입니다\n둘째 줄 | 끝\t "


@pytest.mark.parametrize("value", [CAPTION, {"x-default": CAPTION, "ko": "다른 말"}, [CAPTION],
                                   {"x-default": " ", "ko": CAPTION}])
def test_title_and_caption_are_the_real_text(value):
    a = _asset("a", "2024-05-01T00:00:00", xmp={"dc": {"title": value, "description": value,
                                                       "subject": [" 바다 | 밤 ", "line\nbreak"]}})
    s = catalog.asset_summary(a)
    assert s["title"] == s["caption"] == "첫 줄입니다\n둘째 줄 | 끝"
    assert s["keywords"] == ["바다 | 밤", "line\nbreak"]
    t = server._summary(a)  # the tools still print one line
    assert t["title"] == t["caption"] == "첫 줄입니다 둘째 줄 / 끝"
    assert t["keywords"] == "바다 / 밤, line break"
    assert server._fmt(t).endswith(" | title: 첫 줄입니다 둘째 줄 / 끝 | caption: 첫 줄입니다 둘째 줄 / 끝")


def test_whitespace_only_text_is_none():
    a = _asset("a", "2024-05-01T00:00:00", xmp={"dc": {"title": " \n\t", "description": {"x-default": ""}}})
    s = catalog.asset_summary(a)
    assert s["title"] is None and s["caption"] is None


def test_album_deleted_after_album_info_was_cached(album, monkeypatch):
    catalog.album_info("x")  # cached for a minute

    def paginate(path):
        raise server.lr._http_error(f"GET {path} returned 404: {{'code': 1002}}", 404)
        yield

    monkeypatch.setattr(catalog.lr, "paginate", paginate)
    with pytest.raises(LrError) as e:
        catalog.album_assets("x")
    assert str(e.value).startswith("Album not found: x") and e.value.status == 404
    assert "x" not in catalog._album_cache  # not trusted any more


def test_other_errors_while_listing_an_album_pass_through(album, monkeypatch):
    def paginate(path):
        raise server.lr._http_error("GET … returned 500: oops", 500)
        yield

    monkeypatch.setattr(catalog.lr, "paginate", paginate)
    with pytest.raises(LrError, match="returned 500") as e:
        catalog.album_assets("x")
    assert e.value.status == 500


def test_http_errors_carry_their_status(monkeypatch):
    monkeypatch.setattr(server.lr, "catalog_path", lambda: "/v2/catalogs/c/")
    monkeypatch.setattr(server.lr, "request", lambda *a, **k: (500, b'{"code": 1}'))
    for call in (lambda: server.lr.rendition("a1", "2048"), lambda: server.lr_create_project_album("n"),
                 lambda: server.lr_add_to_project_album("p", ["a"]), lambda: server.lr_delete_project_album("p"),
                 lambda: server.lr.get_json("/v2/account")):
        with pytest.raises(LrError) as e:
            call()
        assert e.value.status == 500
    monkeypatch.setattr(server.lr, "request", lambda *a, **k: (404, b""))
    with pytest.raises(LrError) as e:
        server.lr.rendition("a1", "fullsize")  # 404 makes it generate; the POST gets 404 too
    assert e.value.status == 404
    assert LrError("plain").status is None
