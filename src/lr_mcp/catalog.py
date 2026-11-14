"""Read-only catalog API: album order and photo metadata as Python data.

Importable without starting the MCP server; the `lr_*` tools are built on these functions.
Stable API (names and return shapes): album_info, album_assets, asset_summary, asset_edited.
Every call goes through lr_mcp.client, so it needs the same sign-in as the server.
"""
import math
import time

from . import client as lr
from .client import LrError

__all__ = ["SORT_MODES", "LrError", "album_assets", "album_info", "asset_edited", "asset_summary"]

SORT_MODES = ("captureDateAsc", "captureDateDesc", "customAsc")
ALBUM_TTL = 60   # seconds an album's sort mode is trusted; the user may be reordering it right now
_album_cache = {}  # album_id -> (time.time() when fetched, album resource)


# ---------------------------------------------------------------- albums

def _dict(v):
    return v if isinstance(v, dict) else {}


def _str(v):
    return v if isinstance(v, str) else None


def _sort_mode(album):
    """payload.assetSortOrder of an album resource; captureDateAsc when missing or not text."""
    return _str(_dict(album.get("payload")).get("assetSortOrder")) or "captureDateAsc"


def _not_found(album_id):
    _album_cache.pop(album_id, None)
    return lr._http_error(f"Album not found: {album_id}. Find album ids with lr_list_albums.", 404)


def _album(album_id):
    ts, a = _album_cache.get(album_id, (0.0, None))
    if a is None or time.time() - ts > ALBUM_TTL:
        try:
            a = lr.get_json(lr.catalog_path() + f"albums/{album_id}")
        except LrError as e:
            if e.status == 404:
                raise _not_found(album_id) from None
            raise
        _album_cache[album_id] = (time.time(), a)
    return a


def album_info(album_id):
    """One album as {"id", "name", "parent", "subtype", "sort"}; every value is a str, or None.

    name: the album's name.
    parent: id of the folder (collection_set) holding it, or None.
    subtype: "collection" (an album), "collection_set" (a folder) or "project" (made through the API).
    sort: payload.assetSortOrder, one of SORT_MODES ("captureDateAsc", "captureDateDesc",
    "customAsc"); "captureDateAsc" when the album has none, and any other text passed through as-is.
    The album is fetched at most once a minute. An unknown id raises LrError("Album not found: ...")
    with status 404.
    """
    a = _album(album_id)
    p = _dict(a.get("payload"))
    return {"id": _str(a.get("id")) or album_id, "name": _str(p.get("name")),
            "parent": _str(_dict(p.get("parent")).get("id")), "subtype": _str(a.get("subtype")), "sort": _sort_mode(a)}


def _album_entries(album_id):
    """Yield {"asset", "order"} for an album's assets in the API's page order (not the album's order).
    A 404 (e.g. the album was deleted since album_info cached it) raises "Album not found"."""
    pages = lr.paginate(lr.catalog_path() + f"albums/{album_id}/assets?embed=asset&limit=500")
    while True:
        try:
            aa = next(pages)
        except StopIteration:
            return
        except LrError as e:
            if e.status == 404:
                raise _not_found(album_id) from None
            raise
        order = (aa.get("payload") or {}).get("order")
        yield {"asset": aa["asset"], "order": None if order is None else str(order)}


def _in_album_order(entries, mode):
    """entries ({"asset", "order", ...}) sorted as an album with sort mode `mode` shows them.
    customAsc: by order key, entries without one last; ties and those keep capture order.
    captureDateDesc: newest capture first. Anything else: oldest capture first."""
    out = sorted(entries, key=lambda e: _str(_dict(e["asset"].get("payload")).get("captureDate")) or "",
                 reverse=mode == "captureDateDesc")
    if mode == "customAsc":
        out.sort(key=lambda e: (e["order"] is None, e["order"] or ""))
    return out


def album_assets(album_id, images_only=True):
    """An album's assets in the album's own order, as a list of {"asset", "order"}.

    asset: the asset resource as the API returns it (pass it to asset_summary / asset_edited).
    order: the album-asset sort key (payload.order) as a string, or None. Keys compare as strings;
    the album's custom order is ascending key order.
    The order follows album_info(album_id)["sort"]: custom order (assets without a key last; those,
    and assets with equal keys, by capture date), or capture date ascending or descending. An unsupported sort mode is treated as
    capture date ascending.
    images_only: leave out videos and other non-image assets.
    Reads the whole album (one request per 500 assets). An unknown or deleted album raises
    LrError("Album not found: ...") with status 404.
    """
    mode = album_info(album_id)["sort"]
    entries = [e for e in _album_entries(album_id) if not images_only or e["asset"].get("subtype") == "image"]
    return _in_album_order(entries, mode)


# ---------------------------------------------------------------- assets

def _num(v):
    """A finite int or float; bools are not numbers here."""
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _pair(v):
    """[numerator, denominator] of finite numbers with a non-zero denominator."""
    return isinstance(v, list) and len(v) == 2 and _num(v[0]) and _num(v[1]) and v[1] != 0


def _float(v):
    """A rational [n, d] or a number as a finite float, else None."""
    if _pair(v):
        v = v[0] / v[1]
    return float(v) if _num(v) else None


def _latest(d, key):
    """ratings/reviews are shaped {user_id: {key, date}}; return the most recent value."""
    entries = [x for x in _dict(d).values() if isinstance(x, dict)]
    if not entries:
        return None
    v = max(entries, key=lambda x: _str(x.get("date")) or "")
    return v.get(key)


def _text(v, depth=0):
    """A dc string: plain, a language alternative {"x-default": ...}, {"description": ...} or a
    one-element list of those. The text as written, stripped at both ends; None when empty."""
    if isinstance(v, list):
        v = v[0] if v else None
    if isinstance(v, dict) and depth < 2:
        xd = v.get("x-default")
        v = xd if isinstance(xd, str) and xd.strip() else next((x for x in v.values() if _text(x, depth + 1)), None)
        return _text(v, depth + 1)
    return (v.strip() or None) if isinstance(v, str) else None


def _keywords(v):
    """dc.subject (a list of strings, a {keyword: true} map or one string) as a list of strings."""
    if isinstance(v, dict):
        v = [k for k, on in v.items() if on]
    if isinstance(v, str):
        v = [v]
    return [t for t in (_text(x) for x in v or []) if t] if isinstance(v, list) else []


def _shutter(v):
    """ExposureTime [n, d] as '1/250' or '30', a number as str(v) ('0.5', '30.0'), text as-is; else None."""
    if _pair(v):
        return f"{v[0]}/{v[1]}" if v[0] < v[1] else f"{v[0] / v[1]:g}"
    if _num(v):
        return str(v)
    return _str(v)


def _iso(v):
    """ISOSpeedRatings: a number or a string of digits, or the first of a list of them (XMP
    stores it as a sequence)."""
    if isinstance(v, list) and v:
        v = v[0]
    if isinstance(v, str) and v.isascii() and v.isdigit():
        return int(v)
    return v if _num(v) else None


def _size(w, h):
    """(width, height) when both are positive finite numbers."""
    return (w, h) if _num(w) and _num(h) and w > 0 and h > 0 else None


def asset_edited(asset):
    """Edit timestamp of an asset resource, or None. develop.userUpdated moves only when develop
    settings (incl. crop) change, so rating/flag edits don't count; falls back to the asset-wide
    userUpdated, then changedAtTime (the first that is non-empty text). lr_download_photos uses it
    to skip unchanged photos."""
    p = _dict(asset.get("payload"))
    for v in (_dict(p.get("develop")).get("userUpdated"), p.get("userUpdated"), p.get("changedAtTime")):
        if isinstance(v, str) and v:
            return v
    return None


def asset_summary(asset):
    """Metadata of an asset resource as plain data. A field the asset lacks, or holds in an
    unexpected type, is None (keywords: []). Numbers are never bools, NaN or infinite.
    A resource without a string id raises LrError.

    id, file (original file name), captured (captureDate as stored, camera-local time),
    flag ("pick", "reject", "unflagged"; latest), camera ("Make Model"), lens: str.
    rating: int 0-5 (latest). iso: number (a string of digits becomes an int). fnumber, exposure_time (seconds), focal_length (mm): float.
    shutter: exposure time as text ("1/250", "30", "0.5").
    original ((width, height) of the original file) and cropped ((width, height) after the crop):
    pairs of positive numbers. orientation: number, the EXIF value (6 and 8 are 90-degree rotations; sizes
    are as stored and not swapped for it). gps: (latitude, longitude) numbers.
    title, caption: str, the text as written (line breaks kept), stripped at both ends; a
    language alternative resolves to x-default. keywords: list of str, each stripped.
    edited: str (asset_edited).
    """
    if not isinstance(asset, dict) or not isinstance(asset.get("id"), str):
        raise LrError("Not an asset resource: it has no string id.")
    p = _dict(asset.get("payload"))
    xmp = _dict(p.get("xmp"))
    exif, tiff, dc = _dict(xmp.get("exif")), _dict(xmp.get("tiff")), _dict(xmp.get("dc"))
    dev, loc, imp = _dict(p.get("develop")), _dict(p.get("location")), _dict(p.get("importSource"))
    cw, ch = dev.get("croppedWidth"), dev.get("croppedHeight")
    ow, oh = imp.get("originalWidth"), imp.get("originalHeight")
    lat, lon = loc.get("latitude"), loc.get("longitude")
    rating = _latest(p.get("ratings"), "rating")
    rating = rating if isinstance(rating, int) and not isinstance(rating, bool) and 0 <= rating <= 5 else None
    return {
        "id": asset["id"],
        "subtype": _str(asset.get("subtype")),
        "file": _str(imp.get("fileName")),
        "captured": _str(p.get("captureDate")),
        "rating": rating,
        "flag": _str(_latest(p.get("reviews"), "flag")),
        "camera": " ".join(v for v in (tiff.get("Make"), tiff.get("Model")) if isinstance(v, str) and v) or None,
        "lens": _str(_dict(xmp.get("aux")).get("Lens")),
        "iso": _iso(exif.get("ISOSpeedRatings")),
        "fnumber": _float(exif.get("FNumber")),
        "shutter": _shutter(exif.get("ExposureTime")),
        "exposure_time": _float(exif.get("ExposureTime")),
        "focal_length": _float(exif.get("FocalLength")),
        "original": _size(ow, oh),
        "cropped": _size(cw, ch),
        "orientation": dev.get("userOrientation") if _num(dev.get("userOrientation")) else None,
        "gps": (lat, lon) if _num(lat) and _num(lon) else None,
        "title": _text(dc.get("title")),
        "caption": _text(dc.get("description")),
        "keywords": _keywords(dc.get("subject")),
        "edited": asset_edited(asset),
    }
