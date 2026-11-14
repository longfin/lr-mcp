"""Tool output on a varied fixture album, compared with tool_output.json.

tool_output.json was written by the tools as they were before the parsing and album-order code
moved to lr_mcp.catalog, so these tests show that the move changed no tool's output.
Regenerate it only for an intended output change: `LR_MCP_WRITE_GOLDEN=1 uv run pytest tests/test_tool_output.py`.
"""
import json
import os
from pathlib import Path

from lr_mcp import server

GOLDEN = Path(__file__).with_name("tool_output.json")


def _asset(aid, captured, **payload):
    return {"id": aid, "subtype": payload.pop("subtype", "image"), "payload": {"captureDate": captured, **payload}}


ASSETS = [
    # (album-asset order key, asset)
    ("V--B", _asset(
        "full", "2024-05-02T18:30:00.000",
        importSource={"fileName": "DSC0001.ARW", "originalWidth": 7008, "originalHeight": 4672},
        xmp={"exif": {"ISOSpeedRatings": 400, "FNumber": [18, 10], "ExposureTime": [1, 125], "FocalLength": [50, 1]},
             "tiff": {"Make": "SONY", "Model": "ILCE-7RM5"}, "aux": {"Lens": "FE 50mm F1.2 GM"},
             "dc": {"title": {"x-default": "Harbour"}, "description": "Boats at dusk |  long\nexposure",
                    "subject": {"sea": True, "boat": True, "old": False}}},
        develop={"croppedWidth": 6000.4, "croppedHeight": 4000, "userOrientation": 6,
                 "userUpdated": "2024-06-01T00:00:00Z"},
        location={"latitude": 35.1, "longitude": 129.04},
        ratings={"u": {"rating": 5, "date": "2024-05-03"}}, reviews={"u": {"flag": "pick", "date": "2024-05-03"}},
        userUpdated="2024-06-02T00:00:00Z",
    )),
    (None, _asset(
        "bare", "2024-05-01T08:00:00",
        importSource={"fileName": "DSC0001.ARW"},
        develop=None, location=None, xmp=None,
    )),
    ("V--A", _asset(
        "long", "2024-05-03T23:59:59",
        importSource={"fileName": "IMG_9.HEIC", "originalWidth": 4032, "originalHeight": 3024},
        xmp={"exif": {"ExposureTime": [30, 1], "FNumber": 8, "FocalLength": [262, 10], "ISOSpeedRatings": 100},
             "tiff": {"Model": "iPhone 15 Pro"},
             "dc": {"title": "  ", "description": [{"en": "Stars"}], "subject": ["night", "sky "]}},
        develop={"croppedWidth": float("nan"), "croppedHeight": 1, "userOrientation": 1},
        ratings={"a": {"rating": 2, "date": "2024-01-01"}, "b": {"rating": 4, "date": "2024-02-01"}},
        reviews={"u": {"flag": "unflagged", "date": "2024-01-01"}},
        changedAtTime="2024-07-01T00:00:00Z",
    )),
    (7, _asset(
        "reject", "2024-05-02T12:00:00",
        importSource={"fileName": "scan.tif", "originalWidth": 2000, "originalHeight": 3000},
        xmp={"exif": {"ExposureTime": 0.5, "FNumber": [0, 0]}, "dc": {"subject": "film", "title": "x" * 80}},
        reviews={"u": {"flag": "reject", "date": "2024-01-01"}},
    )),
    ("V--C", {"id": "clip", "subtype": "video", "payload": {"captureDate": "2024-05-02T13:00:00",
                                                             "importSource": {"fileName": "clip.mov"}}}),
]

CALLS = [
    ("lr_list_photos", {"album_id": "x"}),
    ("lr_list_photos", {"album_id": "x", "sort": "captured"}),
    ("lr_list_photos", {"album_id": "x", "limit": 2}),
    ("lr_list_photos", {"album_id": "x", "min_rating": 3}),
    ("lr_list_photos", {"album_id": "x", "camera": "sony", "hour_from": 18}),
    ("lr_list_photos", {"captured_after": "2024-05-01", "captured_before": "2024-05-02"}),
] + [("lr_get_photo", {"asset_id": a["id"], "include_develop": False}) for _, a in ASSETS]

MODES = ["customAsc", "captureDateAsc", "captureDateDesc", None, "fileNameAsc"]


def _serve(monkeypatch, mode):
    monkeypatch.setattr(server.lr, "catalog_path", lambda: "/v2/catalogs/c/")
    monkeypatch.setattr(server.lr, "access_token", lambda: "token")
    server._albums_cache = (0.0, None)
    server._album_cache.clear()
    by_id = {a["id"]: a for _, a in ASSETS}

    def get_json(path, auth=True, deadline=None):
        if path.endswith("albums/x"):
            return {"id": "x", "payload": {"name": "Trip", **({"assetSortOrder": mode} if mode else {})}}
        if "assets?asset_ids=" in path:
            return {"resources": [by_id[i] for i in path.split("=")[1].split(",")]}
        return by_id[path.rsplit("/", 1)[1]]

    def paginate(path):
        if "/albums/x/assets" in path:
            return iter([{"asset": a, "payload": {} if o is None else {"order": o}} for o, a in ASSETS])
        if path.endswith("albums?limit=500"):
            return iter([{"id": "x", "subtype": "collection", "payload": {"name": "Trip"}}])
        return iter(sorted((a for _, a in ASSETS), key=lambda a: a["payload"]["captureDate"]))

    monkeypatch.setattr(server.lr, "get_json", get_json)
    monkeypatch.setattr(server.lr, "paginate", paginate)
    monkeypatch.setattr(server.lr, "rendition", lambda aid, size: f"{aid}-{size}".encode())


def _outputs(monkeypatch, tmp_path):
    out = {}
    for mode in MODES:
        _serve(monkeypatch, mode)
        for name, kw in CALLS:
            if name == "lr_get_photo" and mode != MODES[0]:
                continue
            key = f"{mode} {name} {json.dumps(kw, sort_keys=True)}"
            out[key] = getattr(server, name)(**kw)
    _serve(monkeypatch, None)
    dest = tmp_path / "dl"
    msg = server.lr_download_photos(album_id="x", size="640", dest_dir=str(dest))
    out["lr_download_photos"] = msg.replace(str(dest), "<dest>")
    out["lr_download_photos files"] = sorted(p.name for p in dest.iterdir())
    out["lr_download_photos sidecar"] = json.loads((dest / server.SIDECAR).read_text())
    return out


def test_tool_output_matches_golden(monkeypatch, tmp_path):
    out = _outputs(monkeypatch, tmp_path)
    if os.environ.get("LR_MCP_WRITE_GOLDEN"):
        GOLDEN.write_text(json.dumps(out, indent=1, ensure_ascii=False, sort_keys=True) + "\n")
    golden = json.loads(GOLDEN.read_text())
    assert out.keys() == golden.keys()
    for key in golden:
        assert out[key] == golden[key], key
