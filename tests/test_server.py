import json
from types import SimpleNamespace

import pytest

from lr_mcp import server
from lr_mcp.client import LrError


def asset(aid, captured, file="IMG.ARW", rating=None, flag=None, iso=100):
    payload = {
        "captureDate": captured,
        "importSource": {"fileName": file, "originalWidth": 6000, "originalHeight": 4000},
        "xmp": {
            "exif": {"ISOSpeedRatings": iso, "FNumber": [28, 10], "ExposureTime": [1, 250],
                     "FocalLength": [35, 1]},
            "tiff": {"Make": "SONY", "Model": "ILCE-7M4"},
            "aux": {"Lens": "FE 35mm F1.4 GM"},
        },
    }
    if rating:
        payload["ratings"] = {"u1": {"rating": 1, "date": "2020-01-01"},
                              "u2": {"rating": rating, "date": "2024-01-01"}}
    if flag:
        payload["reviews"] = {"u1": {"flag": flag, "date": "2024-01-01"}}
    return {"id": aid, "subtype": "image", "payload": payload}


@pytest.fixture(autouse=True)
def catalog(monkeypatch):
    monkeypatch.setattr(server.lr, "catalog_path", lambda: "/v2/catalogs/c/")
    server._albums_cache = (0.0, None)
    server._album_cache.clear()
    monkeypatch.setattr(server.lr, "get_json", lambda path: {"id": "x", "payload": {}})


def test_summary():
    s = server._summary(asset("a1", "2024-05-01T10:00:00", rating=4, flag="pick"))
    assert s["rating"] == 4  # most recent rating wins
    assert s["flag"] == "pick"
    assert s["aperture"] == "f/2.8"
    assert s["shutter"] == "1/250"
    assert s["focal_mm"] == 35
    assert s["camera"] == "SONY ILCE-7M4"
    assert s["size"] == "6000x4000"


def test_summary_long_exposure():
    a = asset("a1", "2024-05-01T10:00:00")
    a["payload"]["xmp"]["exif"]["ExposureTime"] = [30, 1]
    assert server._summary(a)["shutter"] == "30"


def test_album_paths(monkeypatch):
    albums = [
        {"id": "f", "subtype": "collection_set", "payload": {"name": "Travel"}},
        {"id": "a", "subtype": "collection", "payload": {"name": "Kyoto", "parent": {"id": "f"}}},
        {"id": "p", "subtype": "project", "payload": {"name": "Picks"}},
    ]
    monkeypatch.setattr(server.lr, "paginate", lambda path: iter(albums))
    assert server._album_paths() == {"f": "Travel", "a": "Travel / Kyoto", "p": "Picks"}
    out = server.lr_list_albums(query="kyoto")
    assert out == "1 albums\nTravel / Kyoto  (a)"
    assert "[folder] Travel" in server.lr_list_albums()
    assert "[project] Picks" in server.lr_list_albums()


def test_list_photos_catalog_stops_at_captured_before(monkeypatch):
    seen = []

    def paginate(path):
        seen.append(path)
        for i, day in enumerate(["01", "02", "03", "04"]):
            yield asset(f"a{i}", f"2024-05-{day}T12:00:00")
        pytest.fail("should stop before exhausting the catalog")

    monkeypatch.setattr(server.lr, "paginate", paginate)
    out = server.lr_list_photos(captured_after="2024-05-01", captured_before="2024-05-02")
    assert "captured_after=2024-05-01T00:00:00" in seen[0]
    assert "captured_before" not in seen[0]  # the API rejects both together
    assert out.splitlines()[0].startswith("2 photos")


def test_list_photos_album_filters(monkeypatch):
    items = [
        asset("late", "2024-05-03T00:00:00", rating=5),
        asset("early", "2024-05-01T00:00:00", rating=5, iso=6400),
        asset("low", "2024-05-02T00:00:00", rating=2),
        asset("rejected", "2024-05-02T00:00:00", rating=5, flag="reject"),
    ]
    monkeypatch.setattr(server.lr, "paginate", lambda path: ({"asset": a} for a in items))
    out = server.lr_list_photos(album_id="x", min_rating=4)
    ids = [line.split(" | ")[0] for line in out.splitlines()[1:]]
    assert ids == ["early", "rejected", "late"]  # sorted by capture date
    out = server.lr_list_photos(album_id="x", min_rating=4, flag="reject")
    assert [line.split(" | ")[0] for line in out.splitlines()[1:]] == ["rejected"]
    out = server.lr_list_photos(album_id="x", min_iso=3200)
    assert [line.split(" | ")[0] for line in out.splitlines()[1:]] == ["early"]


def test_add_to_project_album_chunks_and_reports_errors(monkeypatch):
    calls = []

    def request(method, path, body=None, **kw):
        calls.append(body)
        ids = [r["id"] for r in body["resources"]]
        resp = {"resources": [{"id": i} for i in ids if i != "bad"],
                "errors": [{"id": "bad", "description": "Asset not found"}] if "bad" in ids else []}
        return 201, b"while (1) {}" + json.dumps(resp).encode()

    monkeypatch.setattr(server.lr, "request", request)
    ids = [f"id{i}" for i in range(59)] + ["bad"]
    out = server.lr_add_to_project_album("alb", ids)
    assert [len(c["resources"]) for c in calls] == [50, 10]
    assert out.startswith("Added 59 photos, 1 failed")
    assert "bad: Asset not found" in out


def test_add_to_project_album_rejects_user_album(monkeypatch):
    monkeypatch.setattr(server.lr, "request", lambda *a, **k: (404, b'while (1) {}{"code": 1002}'))
    with pytest.raises(LrError, match="404"):
        server.lr_add_to_project_album("alb", ["a"])


def test_view_photos_rejects_bad_size():
    with pytest.raises(LrError):
        server.lr_view_photos(["a"], size="fullsize")


def test_download_requires_target():
    with pytest.raises(LrError):
        server.lr_download_photos()


def _ids(out):
    return [line.split(" | ")[0] for line in out.splitlines()[1:]]


@pytest.mark.parametrize("hour_from, hour_to, expected", [
    (19, 4, ["late", "midnight", "dawn"]),  # wraps past midnight
    (12, 15, ["noon"]),
    (19, None, ["late"]),
    (None, 4, ["midnight", "dawn"]),
])
def test_list_photos_time_of_day(monkeypatch, hour_from, hour_to, expected):
    items = [
        asset("midnight", "2024-05-01T00:30:00"),
        asset("dawn", "2024-05-01T04:59:00"),
        asset("morning", "2024-05-01T05:00:00"),
        asset("noon", "2024-05-01T12:00:00"),
        asset("late", "2024-05-01T20:15:00"),
    ]
    monkeypatch.setattr(server.lr, "paginate", lambda path: ({"asset": a} for a in items))
    out = server.lr_list_photos(album_id="x", hour_from=hour_from, hour_to=hour_to)
    assert sorted(_ids(out)) == sorted(expected)


def test_list_photos_rejects_bad_hour():
    with pytest.raises(LrError):
        server.lr_list_photos(album_id="x", hour_from=24)


def test_list_photos_fnumber(monkeypatch):
    def with_f(aid, f):
        a = asset(aid, "2024-05-01T12:00:00")
        if f is None:
            del a["payload"]["xmp"]["exif"]["FNumber"]
        else:
            a["payload"]["xmp"]["exif"]["FNumber"] = f
        return a

    items = [with_f("f1", [10, 10]), with_f("f2", [2, 1]), with_f("f4", [40, 10]),
             with_f("f8", [8, 1]), with_f("none", None)]
    monkeypatch.setattr(server.lr, "paginate", lambda path: ({"asset": a} for a in items))
    assert _ids(server.lr_list_photos(album_id="x", max_fnumber=2)) == ["f1", "f2"]
    assert _ids(server.lr_list_photos(album_id="x", min_fnumber=8)) == ["f8"]
    assert _ids(server.lr_list_photos(album_id="x", min_fnumber=2, max_fnumber=4)) == ["f2", "f4"]
    assert len(_ids(server.lr_list_photos(album_id="x"))) == 5


def test_lr_login_reports_port_fallback(monkeypatch):
    import asyncio

    def login(timeout, on_url, on_notice):
        on_notice("Port 8000 is busy; using port 54321.")
        return {"refresh_token": "rt"}

    monkeypatch.setattr(server.lr, "login", login)
    out = asyncio.run(server.lr_login())
    assert out.startswith("Signed in (refresh token: yes).") and "Port 8000 was busy" in out


def test_list_photos_stops_at_scan_cap_without_matches(monkeypatch):
    monkeypatch.setattr(server, "MAX_SCAN", 5)
    pulled = []

    def paginate(path):
        for i in range(100):
            pulled.append(i)
            yield asset(f"a{i}", f"2024-05-01T12:{i:02d}:00")

    monkeypatch.setattr(server.lr, "paginate", paginate)
    out = server.lr_list_photos(captured_after="2024-05-01", flag="reject")
    assert out.splitlines()[0].startswith("0 photos (scanned 5, scan limit reached")
    assert len(pulled) == 5


def test_list_photos_no_cap_note_when_catalog_ends(monkeypatch):
    monkeypatch.setattr(server, "MAX_SCAN", 5)
    items = [asset(f"a{i}", "2024-05-01T12:00:00") for i in range(3)]
    monkeypatch.setattr(server.lr, "paginate", lambda path: iter(items))
    assert server.lr_list_photos(captured_after="2024-05-01").splitlines()[0] == "3 photos (scanned 3)"


def test_list_photos_stops_at_time_budget(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(server.time, "monotonic", lambda: clock[0])
    pulled = []

    def paginate(path):
        for i in range(100):
            pulled.append(i)
            clock[0] += server.SCAN_SECONDS / 4  # each asset costs a quarter of the budget
            yield asset(f"a{i}", f"2024-05-01T12:{i:02d}:00")

    monkeypatch.setattr(server.lr, "paginate", paginate)
    out = server.lr_list_photos(captured_after="2024-05-01", flag="reject")
    assert out.splitlines()[0] == "0 photos (scanned 4, time limit reached — results are partial; narrow with captured_after)"
    assert len(pulled) == 5


def test_list_photos_album_cap_note_has_no_date_hint(monkeypatch):
    monkeypatch.setattr(server, "MAX_SCAN", 2)
    items = [asset(f"a{i}", "2024-05-01T12:00:00") for i in range(3)]
    monkeypatch.setattr(server.lr, "paginate", lambda path: ({"asset": a} for a in items))
    head = server.lr_list_photos(album_id="x", flag="reject").splitlines()[0]
    assert head == "0 photos (scanned 2, album order: captureDateAsc, scan limit reached — results are partial)"


def test_summary_metadata_missing():
    a = asset("a1", "2024-05-01T10:00:00")
    s = server._summary(a)
    for k in ("title", "caption", "keywords", "cropped", "orientation", "gps"):
        assert s[k] is None
    assert server._fmt(s) == "a1 | IMG.ARW | 2024-05-01T10:00:00 | f/2.8 1/250s ISO100 35mm | SONY ILCE-7M4 + FE 35mm F1.4 GM"
    a["payload"]["xmp"]["dc"] = {"title": "  ", "description": {}, "subject": []}
    a["payload"]["location"] = {"direction": 120.5}
    assert all(server._summary(a)[k] is None for k in ("title", "caption", "keywords", "gps"))


@pytest.mark.parametrize("caption", [
    "Harbour at dusk",
    {"description": "Harbour at dusk"},
    {"x-default": "Harbour at dusk"},
    "Harbour\n  at\tdusk ",
])
def test_summary_caption_forms(caption):
    a = asset("a1", "2024-05-01T10:00:00")
    a["payload"]["xmp"]["dc"] = {"title": "Busan", "description": caption}
    s = server._summary(a)
    assert s["title"] == "Busan"
    assert s["caption"] == "Harbour at dusk"


@pytest.mark.parametrize("subject, expected", [
    (["sea", "night"], "sea, night"),
    ({"sea": True, "night": True, "old": False}, "sea, night"),
    ("sea", "sea"),
])
def test_summary_keywords(subject, expected):
    a = asset("a1", "2024-05-01T10:00:00")
    a["payload"]["xmp"]["dc"] = {"subject": subject}
    assert server._summary(a)["keywords"] == expected


def test_summary_crop_orientation_gps():
    a = asset("a1", "2024-05-01T10:00:00")
    a["payload"]["develop"] = {"croppedWidth": 5400, "croppedHeight": 3037.6, "userOrientation": 8}
    a["payload"]["location"] = {"latitude": 35.1, "longitude": 129.04, "direction": 90}
    s = server._summary(a)
    assert (s["cropped"], s["orientation"], s["gps"], s["size"]) == ("5400x3038", 8, "35.1,129.04", "6000x4000")


def test_fmt_appends_title_and_caption():
    a = asset("a1", "2024-05-01T10:00:00", rating=3)
    a["payload"]["xmp"]["dc"] = {"title": "Busan\nnight", "description": {"x-default": "word " * 30}}
    s = server._summary(a)
    line = server._fmt(s)
    assert "\n" not in line
    assert line.startswith("a1 | IMG.ARW | 2024-05-01T10:00:00 | ★★★ | f/2.8 1/250s ISO100 35mm"
                           " | SONY ILCE-7M4 + FE 35mm F1.4 GM | title: Busan night | caption: word word")
    caption = line.split(" | caption: ")[1]
    assert len(caption) == 60 and caption.endswith("…")
    s["title"] = None
    assert " | title: " not in server._fmt(s) and " | caption: " in server._fmt(s)


def test_get_photo_lists_metadata(monkeypatch):
    a = asset("a1", "2024-05-01T10:00:00")
    a["payload"]["xmp"]["dc"] = {"title": "Busan", "subject": ["sea"]}
    a["payload"]["develop"] = {"croppedWidth": 4000, "croppedHeight": 3000, "userOrientation": 1}
    monkeypatch.setattr(server.lr, "get_json", lambda path: a)
    lines = server.lr_get_photo("a1", include_develop=False).splitlines()
    assert {"title: Busan", "keywords: sea", "cropped: 4000x3000", "orientation: 1"} <= set(lines)
    assert not any(l.startswith(("caption:", "gps:")) for l in lines)


def test_summary_survives_null_sections_and_odd_numbers():
    a = asset("a1", "2024-05-01T12:00:00")
    a["payload"]["develop"] = {"croppedWidth": float("nan"), "croppedHeight": 10, "userOrientation": float("inf")}
    a["payload"]["location"] = None
    a["payload"]["xmp"]["dc"] = None
    s = server._summary(a)
    assert s["cropped"] is None and s["orientation"] is None and s["gps"] is None and s["title"] is None
    a["payload"]["xmp"] = None
    s = server._summary(a)
    assert s["lens"] is None and s["aperture"] is None and server._fnumber(a) is None
    a["payload"]["xmp"] = {"exif": {"FocalLength": float("inf"), "FNumber": float("nan")}, "aux": None}
    s = server._summary(a)
    assert s["focal_mm"] is None and s["aperture"] is None and s["lens"] is None


@pytest.mark.parametrize("value, expected", [
    ({"x-default": "  ", "en": "Hi"}, "Hi"),
    ({"description": {"x-default": "nested"}}, "nested"),
    (["first", "second"], "first"),
    ([{"x-default": "in list"}], "in list"),
    ("Busan | night", "Busan / night"),
    ([], None),
    ({"x-default": ""}, None),
])
def test_text_handles_alternative_shapes(value, expected):
    assert server._text(value) == expected


def _album(monkeypatch, mode, items):
    """items: (asset, order) pairs as album-asset resources; the album has assetSortOrder=mode."""
    gets = []

    def get_json(path):
        gets.append(path)
        return {"id": "x", "payload": {"assetSortOrder": mode} if mode else {}}

    monkeypatch.setattr(server.lr, "get_json", get_json)
    monkeypatch.setattr(server.lr, "paginate", lambda path: (
        {"asset": a, "payload": {"order": o} if o else {}} for a, o in items))
    return gets


ALBUM = [
    (asset("b", "2024-05-02T00:00:00"), "V--A"),
    (asset("nokey1", "2024-05-05T00:00:00"), None),
    (asset("c", "2024-05-01T00:00:00"), "V--K"),
    (asset("nokey2", "2024-05-04T00:00:00"), None),
    (asset("a", "2024-05-03T00:00:00"), "V--3"),
]


def test_list_photos_album_custom_order(monkeypatch):
    gets = _album(monkeypatch, "customAsc", ALBUM)
    out = server.lr_list_photos(album_id="x")
    assert _ids(out) == ["a", "b", "c", "nokey2", "nokey1"]  # missing order last, by capture
    assert out.splitlines()[0] == "5 photos (scanned 5, album order: customAsc)"
    assert gets == ["/v2/catalogs/c/albums/x"]
    server.lr_list_photos(album_id="x")
    assert len(gets) == 1  # cached


def test_list_photos_album_uses_cached_album_listing(monkeypatch):
    gets = _album(monkeypatch, None, ALBUM)
    server._albums_cache = (server.time.time(), [{"id": "x", "payload": {"assetSortOrder": "customAsc"}}])
    assert _ids(server.lr_list_photos(album_id="x"))[:3] == ["a", "b", "c"]
    assert gets == []


def test_list_photos_album_capture_desc(monkeypatch):
    _album(monkeypatch, "captureDateDesc", ALBUM)
    out = server.lr_list_photos(album_id="x")
    assert _ids(out) == ["nokey1", "nokey2", "a", "b", "c"]
    assert "album order: captureDateDesc" in out.splitlines()[0]


@pytest.mark.parametrize("mode", ["captureDateAsc", None])
def test_list_photos_album_capture_asc_default(monkeypatch, mode):
    _album(monkeypatch, mode, ALBUM)
    out = server.lr_list_photos(album_id="x")
    assert _ids(out) == ["c", "b", "a", "nokey2", "nokey1"]
    assert "album order: captureDateAsc" in out.splitlines()[0]


def test_list_photos_sort_captured_overrides_album(monkeypatch):
    _album(monkeypatch, "customAsc", ALBUM)
    out = server.lr_list_photos(album_id="x", sort="captured")
    assert _ids(out) == ["c", "b", "a", "nokey2", "nokey1"]
    assert out.splitlines()[0] == "5 photos (scanned 5, album order: customAsc, sorted by capture)"


def test_list_photos_album_limit_after_sort(monkeypatch):
    _album(monkeypatch, "customAsc", ALBUM)
    out = server.lr_list_photos(album_id="x", limit=2)
    assert _ids(out) == ["a", "b"]  # not the first two by API page order
    assert out.splitlines()[0] == "2 photos (scanned 5, album order: customAsc, limit reached)"
    assert _ids(server.lr_list_photos(album_id="x", sort="captured", limit=2)) == ["c", "b"]


def test_list_photos_sort_ignored_without_album(monkeypatch):
    items = [asset("a0", "2024-05-01T12:00:00"), asset("a1", "2024-05-02T12:00:00")]
    monkeypatch.setattr(server.lr, "paginate", lambda path: iter(items))
    monkeypatch.setattr(server.lr, "get_json", lambda path: pytest.fail("no album lookup"))
    out = server.lr_list_photos(captured_after="2024-05-01", sort="captured")
    assert out.splitlines()[0] == "2 photos (scanned 2)"
    assert _ids(out) == ["a0", "a1"]


def test_list_photos_rejects_bad_sort():
    with pytest.raises(LrError, match="sort"):
        server.lr_list_photos(album_id="x", sort="custom")


def test_album_sort_order_ignores_stale_album_list(monkeypatch):
    stale = [{"id": "x", "payload": {"assetSortOrder": "captureDateAsc"}}]
    monkeypatch.setattr(server, "_albums_cache", (server.time.time() - server.ALBUM_TTL - 1, stale))
    monkeypatch.setattr(server.lr, "get_json", lambda path: {"id": "x", "payload": {"assetSortOrder": "customAsc"}})
    assert server._album_sort_order("x") == "customAsc"
    monkeypatch.setattr(server, "_albums_cache", (server.time.time(), stale))
    server._album_cache.clear()
    assert server._album_sort_order("x") == "captureDateAsc"


def test_list_photos_coerces_non_string_order_keys(monkeypatch):
    monkeypatch.setattr(server.lr, "get_json", lambda path: {"id": "x", "payload": {"assetSortOrder": "customAsc"}})
    items = [({"order": 3}, asset("int", "2024-05-01T12:00:00")), ({"order": "V--A"}, asset("str", "2024-05-01T12:01:00"))]
    monkeypatch.setattr(server.lr, "paginate", lambda path: ({"asset": a, "payload": o} for o, a in items))
    assert _ids(server.lr_list_photos(album_id="x")) == ["int", "str"]


def test_list_photos_flags_unsupported_album_order(monkeypatch):
    monkeypatch.setattr(server.lr, "get_json", lambda path: {"id": "x", "payload": {"assetSortOrder": "fileNameAsc"}})
    items = [asset("b", "2024-05-02T12:00:00"), asset("a", "2024-05-01T12:00:00")]
    monkeypatch.setattr(server.lr, "paginate", lambda path: ({"asset": a} for a in items))
    out = server.lr_list_photos(album_id="x")
    assert out.splitlines()[0] == "2 photos (scanned 2, album order: fileNameAsc (unsupported, sorted by capture))"
    assert _ids(out) == ["a", "b"]
    out = server.lr_list_photos(album_id="x", sort="captured", limit=1)
    assert out.splitlines()[0] == "1 photos (scanned 2, album order: fileNameAsc (unsupported, sorted by capture), limit reached)"


def test_list_photos_rejects_bad_limit():
    with pytest.raises(LrError, match="limit"):
        server.lr_list_photos(album_id="x", limit=0)


@pytest.fixture
def lib(monkeypatch):
    """Two photos (same stem, so the second is IMG-2.jpg) and a video; counts rendition calls."""
    items = [asset("a1", "2024-05-01T12:00:00"), asset("a2", "2024-05-01T12:01:00"),
             {"id": "v1", "subtype": "video", "payload": {}}]
    for a in items[:2]:
        a["payload"]["develop"] = {"userUpdated": "2024-06-01T00:00:00Z"}
    lib = SimpleNamespace(items=items, calls=[], fail=set())

    def rendition(aid, size):
        lib.calls.append(aid)
        if aid in lib.fail:
            raise LrError("generation timed out")
        return f"{aid}-{size}".encode()

    monkeypatch.setattr(server.lr, "get_json", lambda path: {"resources": items})
    monkeypatch.setattr(server.lr, "rendition", rendition)
    return lib


def _dl(tmp_path, **kw):
    return server.lr_download_photos(asset_ids=["a1", "a2", "v1"], dest_dir=str(tmp_path), **kw)


def test_download_writes_files_and_sidecar(lib, tmp_path):
    out = _dl(tmp_path, size="2560")
    assert out.splitlines()[0] == f"Saved 2 photos to {tmp_path}"
    assert "Skipped 1 non-image" in out
    assert (tmp_path / "IMG-2.jpg").read_bytes() == b"a2-2560"
    side = json.loads((tmp_path / ".lr-mcp.json").read_text())
    assert side["IMG.jpg"] == {"asset_id": "a1", "size": "2560", "updated": "2024-06-01T00:00:00Z", "bytes": 7}
    assert side["IMG-2.jpg"]["asset_id"] == "a2"


def test_download_skips_unchanged(lib, tmp_path):
    _dl(tmp_path)
    lib.calls.clear()
    out = _dl(tmp_path)
    assert lib.calls == []
    assert out.splitlines()[0] == f"Saved 0 photos to {tmp_path}; 2 unchanged since the last download, not fetched again"
    assert "Skipped 1 non-image" in out


def test_download_redownloads_when_edited(lib, tmp_path):
    _dl(tmp_path)
    lib.calls.clear()
    lib.items[1]["payload"]["develop"]["userUpdated"] = "2024-07-01T00:00:00Z"
    out = _dl(tmp_path)
    assert lib.calls == ["a2"]
    assert out.splitlines()[0] == f"Saved 1 photos to {tmp_path}; 1 unchanged since the last download, not fetched again"
    side = json.loads((tmp_path / ".lr-mcp.json").read_text())
    assert side["IMG-2.jpg"]["updated"] == "2024-07-01T00:00:00Z"


def test_download_redownloads_other_size_deleted_file_or_force(lib, tmp_path):
    _dl(tmp_path)
    lib.calls.clear()
    _dl(tmp_path, size="fullsize")
    assert sorted(lib.calls) == ["a1", "a2"]
    lib.calls.clear()
    (tmp_path / "IMG.jpg").unlink()
    _dl(tmp_path, size="fullsize")
    assert lib.calls == ["a1"]
    lib.calls.clear()
    _dl(tmp_path, size="fullsize", force=True)
    assert sorted(lib.calls) == ["a1", "a2"]


def test_download_redownloads_truncated_file_or_missing_timestamp(lib, tmp_path):
    del lib.items[0]["payload"]["develop"]
    _dl(tmp_path)
    lib.calls.clear()
    (tmp_path / "IMG-2.jpg").write_bytes(b"x")
    _dl(tmp_path)
    assert sorted(lib.calls) == ["a1", "a2"]


def test_download_ignores_corrupt_sidecar(lib, tmp_path):
    (tmp_path / ".lr-mcp.json").write_text("{not json")
    assert _dl(tmp_path).splitlines()[0] == f"Saved 2 photos to {tmp_path}"
    assert set(json.loads((tmp_path / ".lr-mcp.json").read_text())) == {"IMG.jpg", "IMG-2.jpg"}


def test_download_keeps_other_entries_and_skips_failures(lib, tmp_path):
    (tmp_path / ".lr-mcp.json").write_text(json.dumps({"OTHER.jpg": {"asset_id": "z", "size": "640",
                                                                       "updated": "t", "bytes": 1}}))
    lib.fail.add("a2")
    out = _dl(tmp_path)
    assert out.splitlines()[0] == f"Saved 1 photos to {tmp_path}"
    assert "1 failed:\na2: generation timed out" in out
    assert set(json.loads((tmp_path / ".lr-mcp.json").read_text())) == {"OTHER.jpg", "IMG.jpg"}
    lib.fail.clear()
    lib.calls.clear()
    _dl(tmp_path)
    assert lib.calls == ["a2"]
    assert not list(tmp_path.glob("*.tmp"))


def test_download_suffix_shift_does_not_skip_wrong_asset(tmp_path, lib):
    server.lr_download_photos(asset_ids=["a1", "a2"], size="2560", dest_dir=str(tmp_path))
    lib.calls.clear()
    del lib.items[0]  # a1 left the selection: a2 now maps to IMG.jpg, whose entry belongs to a1
    out = server.lr_download_photos(asset_ids=["a2"], size="2560", dest_dir=str(tmp_path))
    assert lib.calls == ["a2"] and "unchanged" not in out


def test_download_os_error_is_reported_and_others_recorded(tmp_path, lib, monkeypatch):
    real = server.lr.rendition

    def rendition(aid, size):
        if aid == "a1":
            raise OSError(28, "No space left on device")
        return real(aid, size)

    monkeypatch.setattr(server.lr, "rendition", rendition)
    out = server.lr_download_photos(asset_ids=["a1", "a2"], size="2560", dest_dir=str(tmp_path))
    assert "1 failed" in out and "a1: [Errno 28]" in out
    assert set(server._read_sidecar(tmp_path)) == {"IMG-2.jpg"}


def test_download_sidecar_write_failure_is_a_warning(tmp_path, lib, monkeypatch):
    monkeypatch.setattr(server.os, "replace", lambda a, b: (_ for _ in ()).throw(OSError(13, "denied")))
    out = server.lr_download_photos(asset_ids=["a1", "a2"], size="2560", dest_dir=str(tmp_path))
    assert out.startswith("Saved 2 photos") and "Warning: could not write .lr-mcp.json" in out
    assert not list(tmp_path.glob("*.tmp")) and not (tmp_path / server.SIDECAR).exists()
