import json

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
