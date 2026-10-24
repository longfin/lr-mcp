"""Lightroom (cloud) MCP server.

`lr-mcp`        run the stdio MCP server
`lr-mcp login`  sign in from the terminal (opens a browser)
`lr-mcp doctor` check the setup and say what to fix next
"""
import asyncio
import itertools
import re
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from mcp.server.mcpserver import Image, MCPServer

from . import client as lr
from .client import LrError

mcp = MCPServer(
    "lightroom",
    instructions=(
        "Browse a Lightroom (cloud) catalog, look at photos and export them. "
        "Find album ids with lr_list_albums, filter photos with lr_list_photos, then look at "
        "candidates with lr_view_photos. The API does not allow changing edits or ratings of "
        "existing photos, deleting photos, or modifying albums the user created; writes are "
        "limited to project albums created by this server."
    ),
)

# ---------------------------------------------------------------- helpers

_albums_cache = (0.0, None)
MAX_SCAN = 20000  # assets lr_list_photos looks at before giving up


def _albums():
    global _albums_cache
    ts, items = _albums_cache
    if items is None or time.time() - ts > 300:
        items = list(lr.paginate(lr.catalog_path() + "albums?limit=500"))
        _albums_cache = (time.time(), items)
    return items


def _album_paths():
    items = {a["id"]: a for a in _albums()}

    def path(a):
        parent = a["payload"].get("parent", {}).get("id")
        name = a["payload"].get("name", "?")
        return f"{path(items[parent])} / {name}" if parent in items else name

    return {aid: path(a) for aid, a in items.items()}


def _frac(v):
    if isinstance(v, list) and len(v) == 2 and v[1]:
        return v[0] / v[1]
    return v


def _latest(d, key):
    """ratings/reviews are shaped {user_id: {key, date}}; return the most recent value."""
    if not d:
        return None
    v = max(d.values(), key=lambda x: x.get("date", ""))
    return v.get(key)


def _summary(asset):
    p = asset.get("payload", {})
    exif = p.get("xmp", {}).get("exif", {})
    tiff = p.get("xmp", {}).get("tiff", {})
    imp = p.get("importSource", {})
    shutter = exif.get("ExposureTime")
    if isinstance(shutter, list) and len(shutter) == 2 and shutter[1]:
        shutter = f"{shutter[0]}/{shutter[1]}" if shutter[0] < shutter[1] else f"{shutter[0] / shutter[1]:g}"
    fnum = _frac(exif.get("FNumber"))
    focal = _frac(exif.get("FocalLength"))
    return {
        "id": asset["id"],
        "type": asset.get("subtype"),
        "file": imp.get("fileName"),
        "captured": p.get("captureDate"),
        "rating": _latest(p.get("ratings"), "rating"),
        "flag": _latest(p.get("reviews"), "flag"),
        "camera": " ".join(filter(None, [tiff.get("Make"), tiff.get("Model")])) or None,
        "lens": p.get("xmp", {}).get("aux", {}).get("Lens"),
        "iso": exif.get("ISOSpeedRatings"),
        "aperture": f"f/{fnum:g}" if isinstance(fnum, (int, float)) else None,
        "shutter": shutter,
        "focal_mm": round(focal) if isinstance(focal, (int, float)) else None,
        "size": f"{imp.get('originalWidth')}x{imp.get('originalHeight')}" if imp.get("originalWidth") else None,
    }


def _fmt(s):
    bits = [s["id"], s["file"] or "?", (s["captured"] or "?")[:19]]
    if s["rating"]:
        bits.append("★" * s["rating"])
    if s["flag"] and s["flag"] != "unflagged":
        bits.append(s["flag"])
    exp = " ".join(str(x) for x in [s["aperture"], s["shutter"] and f"{s['shutter']}s",
                                    s["iso"] and f"ISO{s['iso']}", s["focal_mm"] and f"{s['focal_mm']}mm"] if x)
    if exp:
        bits.append(exp)
    if s["camera"]:
        bits.append(s["camera"] + (f" + {s['lens']}" if s["lens"] else ""))
    return " | ".join(bits)


def _iter_assets(album_id, captured_after, captured_before):
    if album_id:
        for aa in lr.paginate(lr.catalog_path() + f"albums/{album_id}/assets?embed=asset&limit=500"):
            yield aa["asset"]
        return
    # the API rejects captured_after together with captured_before, so walk forward
    # from captured_after and let the caller stop at captured_before
    q = "assets?limit=500&subtype=image%3Bvideo"
    if captured_after:
        q += "&captured_after=" + captured_after
    yield from lr.paginate(lr.catalog_path() + q)


def _fnumber(asset):
    v = _frac(asset.get("payload", {}).get("xmp", {}).get("exif", {}).get("FNumber"))
    return v if isinstance(v, (int, float)) else None


def _in_hours(captured, hour_from, hour_to):
    """hour_from..hour_to inclusive; wraps past midnight when hour_from > hour_to (e.g. 19..4)."""
    if not captured or len(captured) < 13:
        return False
    h = int(captured[11:13])
    lo = 0 if hour_from is None else hour_from
    hi = 23 if hour_to is None else hour_to
    return lo <= h <= hi if lo <= hi else h >= lo or h <= hi


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------- tools

@mcp.tool()
async def lr_login() -> str:
    """Sign in to Adobe in the browser and store tokens. Only needed when another tool
    reports that the user is not signed in or the token refresh failed.
    Waits up to 5 minutes for the user to finish signing in."""
    tok = await asyncio.to_thread(lr.login, 300, lambda url: None)
    return f"Signed in (refresh token: {'yes' if tok.get('refresh_token') else 'no'})."


@mcp.tool()
def lr_account() -> str:
    """Account info: subscription status and cloud storage usage."""
    d = lr.get_json("/v2/account")
    ent = d.get("entitlement", {})
    st = ent.get("storage", {})
    gib = 2 ** 30
    lines = [f"{d.get('full_name')} <{d.get('email')}>", f"Subscription: {ent.get('status')}"]
    if st:
        lines.append(f"Storage: {st['used'] / gib:,.1f} GiB used of {st['display_limit'] / gib:,.1f} GiB shown"
                     f" (hard limit {st['limit'] / gib:,.1f} GiB, remaining {(st['limit'] - st['used']) / gib:,.2f} GiB)")
    return "\n".join(lines)


@mcp.tool()
def lr_list_albums(query: str | None = None, include_folders: bool = True) -> str:
    """List albums as 'Folder / Album' paths with their ids.
    query: case-insensitive substring of the path, e.g. 'kyoto' or '2024'.
    Folders are tagged [folder]; project albums created by this server are tagged [project]."""
    paths = _album_paths()
    rows = []
    for a in _albums():
        if a["subtype"] == "collection_set" and not include_folders:
            continue
        p = paths[a["id"]]
        if query and query.lower() not in p.lower():
            continue
        tag = {"collection_set": "[folder] ", "project": "[project] "}.get(a["subtype"], "")
        rows.append(f"{tag}{p}  ({a['id']})")
    rows.sort()
    if not rows:
        return "No matching albums."
    return f"{len(rows)} albums\n" + "\n".join(rows)


@mcp.tool()
def lr_list_photos(
    album_id: str | None = None,
    captured_after: str | None = None,
    captured_before: str | None = None,
    min_rating: int | None = None,
    flag: str | None = None,
    camera: str | None = None,
    lens: str | None = None,
    min_iso: int | None = None,
    filename: str | None = None,
    hour_from: int | None = None,
    hour_to: int | None = None,
    max_fnumber: float | None = None,
    min_fnumber: float | None = None,
    limit: int = 100,
) -> str:
    """List photos matching filters, oldest capture first.
    album_id: search inside one album; omit to search the whole catalog
    (then narrow it with captured_after).
    captured_after / captured_before: 'YYYY-MM-DD' or 'YYYY-MM-DDTHH:MM:SS'.
    min_rating: minimum star rating (1-5). flag: 'pick' or 'reject'.
    camera / lens / filename: case-insensitive substrings. min_iso: minimum ISO.
    hour_from / hour_to: time of day in the camera's local capture time, 0-23 inclusive;
    wraps past midnight when hour_from > hour_to (e.g. 19 and 4 for night shots).
    max_fnumber / min_fnumber: f-number bounds, e.g. max_fnumber=2 for f/2 or wider,
    min_fnumber=8 for f/8 or narrower. Useful when lens info is missing (manual lenses).
    Photos without the needed capture time or f-number are excluded when these are set.
    Each line: id | file | captured | rating | flag | exposure | camera + lens"""
    for h in (hour_from, hour_to):
        if h is not None and not 0 <= h <= 23:
            raise LrError("hour_from and hour_to must be between 0 and 23")
    if captured_after and len(captured_after) == 10:
        captured_after += "T00:00:00"
    if captured_before and len(captured_before) == 10:
        captured_before += "T23:59:59"
    out, scanned, capped = [], 0, False
    for a in itertools.islice(_iter_assets(album_id, captured_after, captured_before), MAX_SCAN):
        scanned += 1
        s = _summary(a)
        cap = (s["captured"] or "")[:19]
        if captured_before and cap > captured_before:
            if not album_id:
                break
            continue
        if captured_after and album_id and cap < captured_after:
            continue
        if min_rating and (s["rating"] or 0) < min_rating:
            continue
        if flag and s["flag"] != flag:
            continue
        if camera and camera.lower() not in (s["camera"] or "").lower():
            continue
        if lens and lens.lower() not in (s["lens"] or "").lower():
            continue
        if min_iso and (s["iso"] or 0) < min_iso:
            continue
        if filename and filename.lower() not in (s["file"] or "").lower():
            continue
        if (hour_from is not None or hour_to is not None) and not _in_hours(s["captured"], hour_from, hour_to):
            continue
        if max_fnumber is not None or min_fnumber is not None:
            f = _fnumber(a)
            if f is None or (max_fnumber is not None and f > max_fnumber) \
                    or (min_fnumber is not None and f < min_fnumber):
                continue
        out.append(s)
        if len(out) >= limit:
            break
    else:
        # the loop ran out without a break: either the catalog ended or the scan cap did
        capped = scanned >= MAX_SCAN
    if album_id:
        out.sort(key=lambda s: s["captured"] or "")
    note = ", limit reached" if len(out) >= limit else ""
    if capped:
        note += ", scan limit reached — results are partial"
        if not album_id:
            note += "; narrow with captured_after"
    head = f"{len(out)} photos (scanned {scanned}{note})"
    return head + "\n" + "\n".join(_fmt(s) for s in out)


@mcp.tool()
def lr_get_photo(asset_id: str, include_develop: bool = True) -> str:
    """Details for one photo: capture info, rating/flag, original file, and a summary of its edits."""
    a = lr.get_json(lr.catalog_path() + f"assets/{asset_id}")
    s = _summary(a)
    lines = [f"{k}: {v}" for k, v in s.items() if v is not None]
    imp = a["payload"].get("importSource", {})
    device = re.sub(r" \{.*", "", imp.get("importedOnDevice", "?"))
    lines.append(f"imported: {imp.get('importTimestamp')} on {device}")
    if include_develop:
        status, body = lr.request("GET", lr.catalog_path() + f"assets/{asset_id}/xmp/develop")
        if status == 200:
            attrs = dict(re.findall(r'crs:(\w+)="([^"]*)"', body.decode()))
            keys = ["CameraProfile", "LookName", "WhiteBalance", "Temperature", "Tint",
                    "Exposure2012", "Contrast2012", "Highlights2012", "Shadows2012", "Whites2012",
                    "Blacks2012", "Texture", "Clarity2012", "Dehaze", "Vibrance", "Saturation",
                    "SharpenDetail", "Sharpness", "LuminanceSmoothing", "PostCropVignetteAmount",
                    "GrainAmount", "HasCrop", "CropAngle"]
            dev = {k: attrs[k] for k in keys if k in attrs}
            lines.append("develop: " + ", ".join(f"{k}={v}" for k, v in dev.items()))
            lines.append(f"(key values out of {len(attrs)} XMP settings)")
    return "\n".join(lines)


@mcp.tool()
def lr_view_photos(asset_ids: list[str], size: str = "640") -> list:
    """Fetch photos as images to look at them (JPEG with edits applied). Up to 12 per call.
    size: 'thumbnail2x' (small, for scanning many) | '640' | '1280' | '2048'."""
    if size not in ("thumbnail2x", "640", "1280", "2048"):
        raise LrError("size must be one of thumbnail2x, 640, 1280, 2048")
    ids = asset_ids[:12]
    with ThreadPoolExecutor(6) as ex:
        imgs = list(ex.map(lambda i: lr.rendition(i, size), ids))
    out = []
    for i, data in zip(ids, imgs):
        out.append(f"asset {i}")
        out.append(Image(data=data, format="jpeg"))
    if len(asset_ids) > 12:
        out.append(f"({len(asset_ids) - 12} more not shown; request them in another call)")
    return out


@mcp.tool()
def lr_download_photos(
    asset_ids: list[str] | None = None,
    album_id: str | None = None,
    size: str = "2048",
    dest_dir: str | None = None,
) -> str:
    """Save photos as JPEG files. Pass either asset_ids or album_id (the whole album).
    size: '640' | '1280' | '2048' | '2560' | 'fullsize' (original resolution with edits; slower).
    dest_dir defaults to ~/Downloads/lightroom/<album name or export-timestamp>.
    Files are named after the original file name with a .jpg extension."""
    if size not in ("640", "1280", "2048", "2560", "fullsize"):
        raise LrError("size must be one of 640, 1280, 2048, 2560, fullsize")
    if album_id:
        assets = [aa["asset"] for aa in lr.paginate(lr.catalog_path() + f"albums/{album_id}/assets?embed=asset&limit=500")]
        name = _album_paths().get(album_id, album_id).split(" / ")[-1]
    elif asset_ids:
        assets = []
        for i in range(0, len(asset_ids), 100):
            chunk = ",".join(asset_ids[i:i + 100])
            assets += lr.get_json(lr.catalog_path() + f"assets?asset_ids={chunk}").get("resources", [])
        name = "export-" + datetime.now().strftime("%Y%m%d-%H%M%S")
    else:
        raise LrError("Pass asset_ids or album_id.")
    dest = Path(dest_dir).expanduser() if dest_dir else Path.home() / "Downloads" / "lightroom" / re.sub(r'[/:]', "_", name)
    dest.mkdir(parents=True, exist_ok=True)

    used = set()

    def target(a):
        stem = Path(a["payload"].get("importSource", {}).get("fileName") or a["id"]).stem
        base, n = stem, 1
        while stem in used:
            n += 1
            stem = f"{base}-{n}"
        used.add(stem)
        return dest / f"{stem}.jpg"

    jobs = [(a["id"], target(a)) for a in assets if a.get("subtype") == "image"]
    errors = []

    def fetch(job):
        aid, path = job
        try:
            path.write_bytes(lr.rendition(aid, size))
        except LrError as e:
            errors.append(f"{aid}: {e}")

    with ThreadPoolExecutor(6) as ex:
        list(ex.map(fetch, jobs))
    skipped = len(assets) - len(jobs)
    msg = f"Saved {len(jobs) - len(errors)} photos to {dest}"
    if skipped:
        msg += f"\nSkipped {skipped} non-image assets (videos etc.)"
    if errors:
        msg += f"\n{len(errors)} failed:\n" + "\n".join(errors[:20])
    return msg


@mcp.tool()
def lr_create_project_album(name: str, asset_ids: list[str] | None = None) -> str:
    """Create a project album owned by this server (shown under Connections in Lightroom).
    Adds asset_ids right away if given."""
    album_id = uuid.uuid4().hex
    now = _now()
    status, body = lr.request("PUT", lr.catalog_path() + f"albums/{album_id}", {
        "subtype": "project", "serviceId": lr.client_id(),
        "payload": {"userCreated": now, "userUpdated": now, "name": name,
                    "publishInfo": {"version": 3, "created": now, "updated": now}},
    })
    if status != 201:
        raise LrError(f"Creating the album failed ({status}): {lr.parse(body)}")
    global _albums_cache
    _albums_cache = (0.0, None)
    msg = f"Created album {name} ({album_id})"
    if asset_ids:
        msg += "\n" + lr_add_to_project_album(album_id, asset_ids)
    return msg


@mcp.tool()
def lr_add_to_project_album(album_id: str, asset_ids: list[str]) -> str:
    """Add photos to a project album. Albums the user created cannot be modified."""
    added, errors = 0, []
    for i in range(0, len(asset_ids), 50):
        chunk = asset_ids[i:i + 50]
        status, body = lr.request("PUT", lr.catalog_path() + f"albums/{album_id}/assets",
                                  {"resources": [{"id": a, "payload": {}} for a in chunk]})
        data = lr.parse(body) or {}
        if status not in (201, 403):
            raise LrError(f"Adding photos failed ({status}): {data}")
        # added photos come back in `resources`, failed ones per photo in top-level `errors`
        added += len(data.get("resources", []))
        for e in data.get("errors", []):
            errors.append(f"{e.get('id')}: {e.get('description')} {e.get('errors', '')}")
    msg = f"Added {added} photos"
    if errors:
        msg += f", {len(errors)} failed:\n" + "\n".join(errors[:20])
    return msg


@mcp.tool()
def lr_delete_project_album(album_id: str) -> str:
    """Delete a project album created by this server. Only the album is removed; photos stay in the catalog."""
    status, body = lr.request("DELETE", lr.catalog_path() + f"albums/{album_id}")
    if status != 204:
        raise LrError(f"Deleting the album failed ({status}): {lr.parse(body)}")
    global _albums_cache
    _albums_cache = (0.0, None)
    return f"Deleted album {album_id}"


def main():
    if sys.argv[1:] == ["login"]:
        try:
            tok = lr.login(on_url=lambda u: print("Sign in from your browser:\n" + u))
        except LrError as e:
            sys.exit(str(e))
        print(f"Tokens saved to {lr.TOKEN_FILE} (refresh token: {'yes' if tok.get('refresh_token') else 'no'})")
        return
    if sys.argv[1:] == ["doctor"]:
        from .doctor import run
        sys.exit(0 if run() else 1)
    if sys.argv[1:]:
        sys.exit(__doc__)
    mcp.run()


if __name__ == "__main__":
    main()
