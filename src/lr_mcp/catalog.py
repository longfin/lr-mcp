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

def _sort_mode(album):
    """payload.assetSortOrder of an album resource; captureDateAsc when missing."""
    return (album.get("payload") or {}).get("assetSortOrder") or "captureDateAsc"


def _album(album_id):
    ts, a = _album_cache.get(album_id, (0.0, None))
    if a is None or time.time() - ts > ALBUM_TTL:
        try:
            a = lr.get_json(lr.catalog_path() + f"albums/{album_id}")
        except LrError as e:
            if e.status == 404:
                raise LrError(f"Album not found: {album_id}. Find album ids with lr_list_albums.") from None
            raise
        _album_cache[album_id] = (time.time(), a)
    return a


def album_info(album_id):
    """One album as {"id", "name", "parent", "subtype", "sort"}.

    parent: id of the folder (collection_set) holding it, or None.
    subtype: "collection" (an album), "collection_set" (a folder) or "project" (made through the API).
    sort: payload.assetSortOrder, one of SORT_MODES ("captureDateAsc", "captureDateDesc",
    "customAsc"); "captureDateAsc" when the album has none, and any other value passed through as-is.
    The album is fetched at most once a minute. An unknown id raises LrError("Album not found: ...").
    """
    a = _album(album_id)
    p = a.get("payload") or {}
    return {"id": a.get("id", album_id), "name": p.get("name"), "parent": (p.get("parent") or {}).get("id"),
            "subtype": a.get("subtype"), "sort": _sort_mode(a)}


def _album_entries(album_id):
    """Yield {"asset", "order"} for an album's assets in the API's page order (not the album's order)."""
    for aa in lr.paginate(lr.catalog_path() + f"albums/{album_id}/assets?embed=asset&limit=500"):
        order = (aa.get("payload") or {}).get("order")
        yield {"asset": aa["asset"], "order": None if order is None else str(order)}


def _in_album_order(entries, mode):
    """entries ({"asset", "order", ...}) sorted as an album with sort mode `mode` shows them.
    customAsc: by order key, entries without one last; ties and those keep capture order.
    captureDateDesc: newest capture first. Anything else: oldest capture first."""
    out = sorted(entries, key=lambda e: (e["asset"].get("payload") or {}).get("captureDate") or "",
                 reverse=mode == "captureDateDesc")
    if mode == "customAsc":
        out.sort(key=lambda e: (e["order"] is None, e["order"] or ""))
    return out


def album_assets(album_id, images_only=True):
    """An album's assets in the album's own order, as a list of {"asset", "order"}.

    asset: the asset resource as the API returns it (pass it to asset_summary / asset_edited).
    order: the album-asset sort key (payload.order) as a string, or None. Keys compare as strings;
    the album's custom order is ascending key order.
    The order follows album_info(album_id)["sort"]: custom order (assets without a key last, by
    capture date), or capture date ascending or descending. An unsupported sort mode is treated as
    capture date ascending.
    images_only: leave out videos and other non-image assets.
    Reads the whole album (one request per 500 assets). An unknown id raises LrError("Album not found: ...").
    """
    mode = album_info(album_id)["sort"]
    entries = [e for e in _album_entries(album_id) if not images_only or e["asset"].get("subtype") == "image"]
    return _in_album_order(entries, mode)


# ---------------------------------------------------------------- assets

def _frac(v):
    if isinstance(v, list) and len(v) == 2 and v[1]:
        return v[0] / v[1]
    return v


def _num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _latest(d, key):
    """ratings/reviews are shaped {user_id: {key, date}}; return the most recent value."""
    if not d:
        return None
    v = max(d.values(), key=lambda x: x.get("date", ""))
    return v.get(key)


def _text(v, depth=0):
    """A dc string: plain, a language alternative {"x-default": ...}, {"description": ...} or a
    one-element list of those; whitespace and '|' collapsed so it stays on one line."""
    if isinstance(v, list):
        v = v[0] if v else None
    if isinstance(v, dict) and depth < 2:
        xd = v.get("x-default")
        v = xd if isinstance(xd, str) and xd.strip() else next((x for x in v.values() if _text(x, depth + 1)), None)
        return _text(v, depth + 1)
    return (" ".join(v.replace("|", "/").split()) or None) if isinstance(v, str) else None


def _keywords(v):
    """dc.subject (a list of strings, a {keyword: true} map or one string) as a list of strings."""
    if isinstance(v, dict):
        v = [k for k, on in v.items() if on]
    if isinstance(v, str):
        v = [v]
    return [t for t in (_text(x) for x in v or []) if t] if isinstance(v, list) else []


def _shutter(v):
    """ExposureTime [n, d] as '1/250' or '30'; any other value as-is."""
    if isinstance(v, list) and len(v) == 2 and v[1]:
        return f"{v[0]}/{v[1]}" if v[0] < v[1] else f"{v[0] / v[1]:g}"
    return v


def asset_edited(asset):
    """Edit timestamp of an asset resource, or None. develop.userUpdated moves only when develop
    settings (incl. crop) change, so rating/flag edits don't count; falls back to the asset-wide
    userUpdated, then changedAtTime. lr_download_photos uses it to skip unchanged photos."""
    p = asset.get("payload") or {}
    return (p.get("develop") or {}).get("userUpdated") or p.get("userUpdated") or p.get("changedAtTime")


def asset_summary(asset):
    """Metadata of an asset resource as plain data; a field the asset lacks is None.

    id, subtype ("image", "video", ...), file (original file name), captured (captureDate as
    stored, camera-local time), rating (0-5, latest), flag ("pick", "reject", "unflagged"; latest),
    camera ("Make Model"), lens, iso, fnumber (float), shutter (exposure time as text: "1/250",
    "30"), exposure_time (seconds, float), focal_length (mm, float),
    original ((width, height) of the original file), cropped ((width, height) after the crop,
    as floats), orientation (EXIF value; 6 and 8 are 90-degree rotations; sizes are as stored and
    not swapped for it), gps ((latitude, longitude)), title, caption (whitespace collapsed to one
    line, '|' replaced by '/'), keywords (list of strings, [] when none), edited (asset_edited).
    """
    p = asset.get("payload") or {}
    xmp = p.get("xmp") or {}
    exif, tiff, dc = xmp.get("exif") or {}, xmp.get("tiff") or {}, xmp.get("dc") or {}
    dev, loc, imp = p.get("develop") or {}, p.get("location") or {}, p.get("importSource") or {}
    cw, ch = dev.get("croppedWidth"), dev.get("croppedHeight")
    lat, lon = loc.get("latitude"), loc.get("longitude")
    fnum, focal = _frac(exif.get("FNumber")), _frac(exif.get("FocalLength"))
    exposure = _frac(exif.get("ExposureTime"))
    return {
        "id": asset["id"],
        "subtype": asset.get("subtype"),
        "file": imp.get("fileName"),
        "captured": p.get("captureDate"),
        "rating": _latest(p.get("ratings"), "rating"),
        "flag": _latest(p.get("reviews"), "flag"),
        "camera": " ".join(filter(None, [tiff.get("Make"), tiff.get("Model")])) or None,
        "lens": (xmp.get("aux") or {}).get("Lens"),
        "iso": exif.get("ISOSpeedRatings"),
        "fnumber": fnum if _num(fnum) else None,
        "shutter": _shutter(exif.get("ExposureTime")),
        "exposure_time": exposure if _num(exposure) else None,
        "focal_length": focal if _num(focal) else None,
        "original": (imp.get("originalWidth"), imp.get("originalHeight")) if imp.get("originalWidth") else None,
        "cropped": (cw, ch) if _num(cw) and _num(ch) else None,
        "orientation": dev.get("userOrientation") if _num(dev.get("userOrientation")) else None,
        "gps": (lat, lon) if _num(lat) and _num(lon) else None,
        "title": _text(dc.get("title")),
        "caption": _text(dc.get("description")),
        "keywords": _keywords(dc.get("subject")),
        "edited": asset_edited(asset),
    }
