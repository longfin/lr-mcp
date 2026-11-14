"""Lightroom (cloud) MCP server.

`lr-mcp`        run the stdio MCP server
`lr-mcp login`  sign in from the terminal (opens a browser)
`lr-mcp doctor` check the setup and say what to fix next
`lr-mcp setup --from <file> [--delete]`
                save the credential from a JSON file downloaded from the Adobe Developer Console
"""
import asyncio
import contextlib
import errno
import itertools
import json
import os
import re
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

try:
    import fcntl
except ImportError:  # Windows
    fcntl = None
    import msvcrt

from mcp.server.mcpserver import Image, MCPServer

from . import catalog
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
MAX_SCAN = 20000   # assets lr_list_photos looks at before giving up
SCAN_SECONDS = 60  # ...or the time it spends, leaving room for one slow page; clients time out at ~120 s
COUNT_SECONDS = 60  # time lr_list_albums spends counting photos, for the same reason


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


_album_cache = catalog._album_cache  # the same dict: one cache for the tools and catalog.album_info
ALBUM_TTL = catalog.ALBUM_TTL
SORT_MODES = catalog.SORT_MODES


def _oneline(v):
    """Text for a one-line listing: whitespace collapsed, '|' (the field separator) replaced by '/'."""
    return (" ".join(v.replace("|", "/").split()) or None) if v else None


def _text(v):
    return _oneline(catalog._text(v))


def _album_sort_order(album_id):
    """Sort mode of one album (see catalog.album_info), from the album listing when it is fresh."""
    ts, items = _albums_cache
    a = next((a for a in items or [] if a["id"] == album_id), None) if time.time() - ts <= ALBUM_TTL else None
    return catalog._sort_mode(a) if a is not None else catalog.album_info(album_id)["sort"]


def _clip(v, n=60):
    return v if len(v) <= n else v[:n - 1] + "…"


def _summary(asset):
    """catalog.asset_summary formatted for the tools: numbers and lists become short strings."""
    d = catalog.asset_summary(asset)
    fnum, focal, size, cropped, gps = d["fnumber"], d["focal_length"], d["original"], d["cropped"], d["gps"]
    return {
        "id": d["id"],
        "type": d["subtype"],
        "file": d["file"],
        "captured": d["captured"],
        "rating": d["rating"],
        "flag": d["flag"],
        "camera": d["camera"],
        "lens": d["lens"],
        "iso": d["iso"],
        "aperture": f"f/{fnum:g}" if fnum is not None else None,
        "shutter": d["shutter"],
        "focal_mm": round(focal) if focal is not None else None,
        "size": f"{size[0]}x{size[1]}" if size else None,
        "cropped": f"{round(cropped[0])}x{round(cropped[1])}" if cropped else None,
        "orientation": d["orientation"],
        "gps": f"{gps[0]},{gps[1]}" if gps else None,
        "title": _oneline(d["title"]),
        "caption": _oneline(d["caption"]),
        "keywords": ", ".join(_oneline(k) for k in d["keywords"]) or None,
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
    for k in ("title", "caption"):
        if s.get(k):
            bits.append(f"{k}: {_clip(s[k])}")
    return " | ".join(bits)


def _iter_assets(album_id, captured_after, captured_before):
    """Yield (asset, order); order is the album-asset payload.order sort key, None outside an album."""
    if album_id:
        for e in catalog._album_entries(album_id):
            yield e["asset"], e["order"]
        return
    # the API rejects captured_after together with captured_before, so walk forward
    # from captured_after and let the caller stop at captured_before
    q = "assets?limit=500&subtype=image%3Bvideo"
    if captured_after:
        q += "&captured_after=" + captured_after
    for a in lr.paginate(lr.catalog_path() + q):
        yield a, None


def _fnumber(asset):
    return catalog.asset_summary(asset)["fnumber"]


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
    Waits up to 5 minutes for the user to finish signing in.
    The callback is https://localhost:8000/callback (or LR_MCP_PORT). If port 8000 is busy, another
    port is used, which needs the redirect URI pattern https://localhost:[0-9]+/callback in the
    Adobe console; with an older pattern such as https://localhost:8000/.* the browser lands on the
    other app at :8000 and sign-in times out. The reply says when this happened."""
    notices = []
    tok = await asyncio.to_thread(lr.login, 300, lambda url: None, notices.append)
    msg = f"Signed in (refresh token: {'yes' if tok.get('refresh_token') else 'no'})."
    if notices:
        msg += f" Port {lr.PORT} was busy, so sign-in used a free port; nothing to fix since it worked."
    return msg


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


def _album_counts(albums):
    """({album_id: count}, note) for the given albums, counted in parallel within COUNT_SECONDS.
    Albums that ran out of time or failed are left out of the dict and explained in the note."""
    # resolve these once here, not in every worker: a token refresh must not run six times at once
    lr.catalog_path()
    lr.access_token()
    deadline = time.monotonic() + COUNT_SECONDS

    def count(a):
        try:
            return lr.album_asset_count(a["id"], a.get("updated"), deadline)
        except Exception as e:  # one bad album or response must not lose the whole listing
            # a request cut short by the deadline fails too; that album is late, not broken
            return None if time.monotonic() > deadline else e

    # workers come back soon after the deadline: requests stop retrying and waiting for data at it
    with ThreadPoolExecutor(6) as ex:
        results = list(ex.map(count, albums))
    counts = {a["id"]: r for a, r in zip(albums, results) if isinstance(r, int)}
    late = sum(r is None for r in results)
    errors = [r for r in results if isinstance(r, Exception)]
    notes = []
    if late:
        notes.append(f"{late} not counted, time limit reached — call again to count the rest "
                     "(finished counts are cached), or narrow with query")
    if errors:
        e = errors[0]
        notes.append(f"{len(errors)} could not be counted: {e if isinstance(e, LrError) else repr(e)}")
    return counts, "; ".join(notes)


@mcp.tool()
def lr_list_albums(query: str | None = None, include_folders: bool = True, include_counts: bool = False) -> str:
    """List albums as 'Folder / Album' paths with their ids.
    query: case-insensitive substring of the path, e.g. 'kyoto' or '2024'.
    Folders are tagged [folder]; project albums created by this server are tagged [project].
    include_counts: append each album's photo count (videos included), e.g. '... (id)  112 photos'.
    Folders get no count. This costs a request per album, so pass a query when you can. Counting
    stops after 60 s; the first line then says how many albums are left, they are listed with
    '? photos', and calling again continues from there (an album of tens of thousands of photos
    may never finish in that time). An album whose count failed is also listed with '? photos'
    and the first line quotes the error. Counts are cached for up to 10 minutes."""
    paths = _album_paths()
    rows = []
    for a in _albums():
        if a["subtype"] == "collection_set" and not include_folders:
            continue
        p = paths[a["id"]]
        if query and query.lower() not in p.lower():
            continue
        tag = {"collection_set": "[folder] ", "project": "[project] "}.get(a["subtype"], "")
        rows.append((f"{tag}{p}  ({a['id']})", a))
    rows.sort(key=lambda r: r[0])
    if not rows:
        return "No matching albums."
    head = f"{len(rows)} albums"
    if not include_counts:
        return head + "\n" + "\n".join(line for line, _ in rows)
    countable = [a for _, a in rows if a["subtype"] != "collection_set"]
    counts, note = _album_counts(countable)
    if note:
        head += f" ({note})"
    lines = []
    for line, a in rows:
        if a["subtype"] != "collection_set":
            n = counts.get(a["id"], "?")
            line += f"  {n} photo{'' if n == 1 else 's'}"
        lines.append(line)
    return head + "\n" + "\n".join(lines)


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
    sort: str = "album",
    limit: int = 100,
) -> str:
    """List photos matching filters, oldest capture first (in an album: the album's order, see sort).
    album_id: search inside one album; omit to search the whole catalog
    (then narrow it with captured_after).
    sort (with album_id only): 'album' (default) keeps the album's own order as set in Lightroom
    (custom order, or capture date ascending/descending); 'captured' forces oldest capture first.
    The first line names the album's order, e.g. 'album order: customAsc'; limit applies after sorting.
    Without album_id, sort is ignored and results are always oldest capture first.
    captured_after / captured_before: 'YYYY-MM-DD' or 'YYYY-MM-DDTHH:MM:SS'.
    min_rating: minimum star rating (1-5). flag: 'pick' or 'reject'.
    camera / lens / filename: case-insensitive substrings. min_iso: minimum ISO.
    hour_from / hour_to: time of day in the camera's local capture time, 0-23 inclusive;
    wraps past midnight when hour_from > hour_to (e.g. 19 and 4 for night shots).
    max_fnumber / min_fnumber: f-number bounds, e.g. max_fnumber=2 for f/2 or wider,
    min_fnumber=8 for f/8 or narrower. Useful when lens info is missing (manual lenses).
    Photos without the needed capture time or f-number are excluded when these are set.
    A scan stops after 20000 assets or 60 s; the first line then says the results are partial,
    so on a large catalog narrow with captured_after up front.
    Each line: id | file | captured | rating | flag | exposure | camera + lens | title: ... | caption: ...
    Empty fields are left out; title and caption are cut to 60 characters (lr_get_photo has them in full)."""
    if sort not in ("album", "captured"):
        raise LrError("sort must be 'album' or 'captured'")
    if limit < 1:
        raise LrError("limit must be at least 1")
    for h in (hour_from, hour_to):
        if h is not None and not 0 <= h <= 23:
            raise LrError("hour_from and hour_to must be between 0 and 23")
    if captured_after and len(captured_after) == 10:
        captured_after += "T00:00:00"
    if captured_before and len(captured_before) == 10:
        captured_before += "T23:59:59"
    mode = _album_sort_order(album_id) if album_id else None
    out, scanned, partial = [], 0, None
    deadline = time.monotonic() + SCAN_SECONDS
    for a, order in itertools.islice(_iter_assets(album_id, captured_after, captured_before), MAX_SCAN):
        if time.monotonic() > deadline:
            partial = "time limit reached"
            break
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
        out.append({"asset": a, "order": order, "summary": s})
        if not album_id and len(out) >= limit:
            break  # an album is sorted after the scan, so it is cut to limit only then
    else:
        # the loop ran out without a break: either the catalog ended or the scan cap did
        if scanned >= MAX_SCAN:
            partial = "scan limit reached"
    if album_id:
        out = catalog._in_album_order(out, mode if sort == "album" else "captureDateAsc")
    out = [e["summary"] for e in out[:limit]]
    note = ", limit reached" if len(out) >= limit else ""
    if album_id:
        label = mode if mode in SORT_MODES else f"{mode} (unsupported, sorted by capture)"
        by_capture = sort == "captured" and mode in SORT_MODES
        note = f", album order: {label}" + (", sorted by capture" if by_capture else "") + note
    if partial:
        note += f", {partial} — results are partial"
        if not album_id:
            note += "; narrow with captured_after"
    head = f"{len(out)} photos (scanned {scanned}{note})"
    return head + "\n" + "\n".join(_fmt(s) for s in out)


@mcp.tool()
def lr_get_photo(asset_id: str, include_develop: bool = True) -> str:
    """Details for one photo: capture info, rating/flag, title, caption, keywords, original file,
    original and cropped pixel size, orientation (EXIF value; 6 and 8 are 90-degree rotations; the
    sizes are as stored and are not swapped for it), GPS when present, and a summary of its edits."""
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


SIDECAR = ".lr-mcp.json"
SIDECAR_LOCK = SIDECAR + ".lock"  # held while the sidecar is merged; kept in the folder
DOWNLOAD_ATTEMPTS = 2  # tries per photo for failures the client has not retried already (see lr.rendition)
GIVE_UP_AFTER = 8      # photos failing in a row that end a download early, e.g. during an outage
STALE_TEMP_SECONDS = 3600  # a temp file of ours this old was left by a killed run
_TEMP = re.compile(r"\..+\.[0-9a-f]{32}\.tmp")  # _write_atomic's temp file names
# flock errors meaning the file system has no locks (some NFS, SMB and FUSE mounts)
_NO_LOCKS = {errno.ENOLCK, errno.ENOSYS, errno.EOPNOTSUPP, getattr(errno, "ENOTSUP", errno.EOPNOTSUPP)}


_edited = catalog.asset_edited  # edit timestamp for the download cache; None means always re-download


def _read_sidecar(dest):
    try:
        d = json.loads((dest / SIDECAR).read_text())
        return {k: v for k, v in d.items() if isinstance(v, dict)} if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_atomic(path, data):
    """Write to a temp file of this writer's own in the same folder, then rename it over `path`, so
    `path` never holds a partial file. The temp file is removed when the write or rename fails."""
    tmp = path.with_name(f".{path.name.lstrip('.')}.{uuid.uuid4().hex}.tmp")  # matches _TEMP
    try:
        with open(tmp, "xb") as f:
            f.write(data)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _write_sidecar(dest, entries):
    """Atomic write; returns an error string instead of raising, since the photos are already saved."""
    try:
        _write_atomic(dest / SIDECAR, json.dumps(entries, indent=1, sort_keys=True).encode())
    except OSError as e:
        return f"could not write {SIDECAR} ({e}); the next call will download these again"


def _open_lock(dest):
    """The lock file, created if needed; read-only when it exists but cannot be written (flock
    does not need write access)."""
    try:
        return open(dest / SIDECAR_LOCK, "a+b")
    except OSError:
        try:
            return open(dest / SIDECAR_LOCK, "rb")
        except OSError:
            pass
        raise


@contextlib.contextmanager
def _folder_lock(dest):
    """Exclusive lock on <dest>/.lr-mcp.json.lock, so two processes saving into one folder do not
    lose each other's sidecar entries. On a file system without locks the caller goes ahead unlocked,
    as before this lock existed."""
    with _open_lock(dest) as f:
        try:
            if fcntl:
                fcntl.flock(f, fcntl.LOCK_EX)
            else:
                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_LOCK, 1)
        except OSError as e:
            if e.errno not in _NO_LOCKS:
                raise
            yield
            return
        try:
            yield
        finally:
            if fcntl:
                fcntl.flock(f, fcntl.LOCK_UN)
            else:
                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)


def _remove_stale_temps(dest):
    """Delete temp files that _write_atomic left behind when a run was killed: only names of that
    exact shape, and only when older than STALE_TEMP_SECONDS, so a running call's are kept."""
    cutoff = time.time() - STALE_TEMP_SECONDS
    try:
        for p in dest.iterdir():
            try:
                if _TEMP.fullmatch(p.name) and p.is_file() and not p.is_symlink() and p.stat().st_mtime < cutoff:
                    p.unlink()
            except OSError:
                pass
    except OSError:
        pass


class _Sidecar:
    """Entries of photos saved by one call, merged into <dest>/.lr-mcp.json as each photo is saved.
    Each merge re-reads the file under the folder lock, so entries written meanwhile by another call
    are kept. A failed merge keeps its entries for the next one, and flush() merges once more at
    the end of the call, also when it is interrupted."""

    def __init__(self, dest):
        self.dest, self.pending = dest, {}
        self.lock = threading.Lock()

    def add(self, name, entry):
        with self.lock:
            self.pending[name] = entry
            self._flush()  # a failure here is retried at the next merge

    def flush(self):
        """Merge what is pending; returns an error string or None."""
        with self.lock:
            return self._flush()

    def _flush(self):
        if not self.pending:
            return None
        try:
            with _folder_lock(self.dest):
                error = _write_sidecar(self.dest, {**_read_sidecar(self.dest), **self.pending})
        except OSError as e:
            error = f"could not write {SIDECAR} ({e}); the next call will download these again"
        if error is None:
            self.pending.clear()
        return error


def _rendition(asset_id, size, may_retry=lambda: True):
    """lr.rendition, tried up to DOWNLOAD_ATTEMPTS times with backoff when the failure is retryable
    (see lr.rendition: only what the client has not retried itself) and may_retry() allows it."""
    for attempt in range(DOWNLOAD_ATTEMPTS):
        try:
            return lr.rendition(asset_id, size)
        except LrError as e:
            if attempt + 1 == DOWNLOAD_ATTEMPTS or not e.retryable or not may_retry():
                raise
        time.sleep(lr._backoff(attempt))


@mcp.tool()
def lr_download_photos(
    asset_ids: list[str] | None = None,
    album_id: str | None = None,
    size: str = "2048",
    dest_dir: str | None = None,
    force: bool = False,
    naming: str = "file",
) -> str:
    """Save photos as JPEG files. Pass either asset_ids or album_id (the whole album).
    size: '640' | '1280' | '2048' | '2560' | 'fullsize' (original resolution with edits; slower).
    dest_dir defaults to ~/Downloads/lightroom/<album name or export-timestamp>.
    naming: 'file' (default) names files after the original file name with a .jpg extension
    (IMG_1.jpg, then IMG_1-2.jpg for a second photo with that name); 'id' names them <asset id>.jpg,
    which stays the same when a photo is renamed.
    Switching naming in one folder keeps both copies; each name has its own cache entry.
    <dest_dir>/.lr-mcp.json records each photo as soon as it is saved, so an interrupted call keeps
    its progress; on the next call into the same folder, photos whose file, size and edit time are
    unchanged are skipped. This needs a stable folder: album_id or an explicit dest_dir (asset_ids
    alone get a new export-<timestamp> folder each time). A .lr-mcp.json.lock file stays next to it.
    force=True re-downloads everything, e.g. if a saved file looks stale.
    Each file is written under a temporary name and renamed when complete, so the folder must be
    writable (in a read-only one every photo fails). A rendition still being generated after a
    minute, or a failed generation request, gets one more try. After 8 photos fail in a row the
    rest are not tried, and the reply says how many; call again to continue."""
    if size not in ("640", "1280", "2048", "2560", "fullsize"):
        raise LrError("size must be one of 640, 1280, 2048, 2560, fullsize")
    if naming not in ("file", "id"):
        raise LrError("naming must be 'file' or 'id'")
    if album_id:
        assets = [e["asset"] for e in catalog._album_entries(album_id)]  # API page order, so -2/-3 suffixes stay as before
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
    _remove_stale_temps(dest)

    used = set()

    def target(a):
        if naming == "id":
            return dest / f"{a['id']}.jpg"
        stem = Path(a["payload"].get("importSource", {}).get("fileName") or a["id"]).stem
        base, n = stem, 1
        while stem in used:
            n += 1
            stem = f"{base}-{n}"
        used.add(stem)
        return dest / f"{stem}.jpg"

    cache = _read_sidecar(dest)

    def fresh(aid, path, updated):
        e = cache.get(path.name)
        if force or not updated or not e or (e.get("asset_id"), e.get("size"), e.get("updated")) != (aid, size, updated):
            return False
        try:
            return path.stat().st_size == e.get("bytes")
        except OSError:
            return False

    jobs, unchanged = [], 0
    for a in assets:
        if a.get("subtype") != "image":
            continue
        aid, path, updated = a["id"], target(a), _edited(a)  # target() for every image keeps -2/-3 stable
        if fresh(aid, path, updated):
            unchanged += 1
        else:
            jobs.append((aid, path, updated))
    errors, saved, sidecar, not_tried = [], {}, _Sidecar(dest), []
    streak, streak_lock = [0], threading.Lock()  # photos failed in a row

    def fetch(job):
        aid, path, updated = job
        with streak_lock:
            if streak[0] >= GIVE_UP_AFTER:
                not_tried.append(aid)
                return
        try:
            data = _rendition(aid, size, lambda: streak[0] < GIVE_UP_AFTER)
            _write_atomic(path, data)
        except (LrError, OSError) as e:
            with streak_lock:
                streak[0] += 1
            errors.append(f"{aid}: {e}")
            return
        with streak_lock:
            streak[0] = 0
        saved[path.name] = {"asset_id": aid, "size": size, "updated": updated, "bytes": len(data)}
        sidecar.add(path.name, saved[path.name])

    try:
        with ThreadPoolExecutor(6) as ex:
            list(ex.map(fetch, jobs))
    finally:
        warning = sidecar.flush()  # also when the call is interrupted
    skipped = len(assets) - len(jobs) - unchanged
    msg = f"Saved {len(saved)} photos to {dest}"
    if unchanged:
        msg += f"; {unchanged} unchanged since the last download, not fetched again"
    if skipped:
        msg += f"\nSkipped {skipped} non-image assets (videos etc.)"
    if errors:
        msg += f"\n{len(errors)} failed:\n" + "\n".join(errors[:20])
    if not_tried:
        msg += (f"\n{len(not_tried)} more not tried: {GIVE_UP_AFTER} photos failed in a row, so the download "
                "stopped early; call again to fetch them")
    if warning:
        msg += f"\nWarning: {warning}"
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
        raise lr._http_error(f"Creating the album failed ({status}): {lr.parse(body)}", status)
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
    try:
        for i in range(0, len(asset_ids), 50):
            chunk = asset_ids[i:i + 50]
            status, body = lr.request("PUT", lr.catalog_path() + f"albums/{album_id}/assets",
                                      {"resources": [{"id": a, "payload": {}} for a in chunk]})
            data = lr.parse(body) or {}
            if status not in (201, 403):
                raise lr._http_error(f"Adding photos failed ({status}): {data}", status)
            # added photos come back in `resources`, failed ones per photo in top-level `errors`
            added += len(data.get("resources", []))
            for e in data.get("errors", []):
                errors.append(f"{e.get('id')}: {e.get('description')} {e.get('errors', '')}")
    finally:
        lr.forget_album_count(album_id)  # also when a later chunk fails after earlier ones were added
    msg = f"Added {added} photos"
    if errors:
        msg += f", {len(errors)} failed:\n" + "\n".join(errors[:20])
    return msg


@mcp.tool()
def lr_delete_project_album(album_id: str) -> str:
    """Delete a project album created by this server. Only the album is removed; photos stay in the catalog."""
    status, body = lr.request("DELETE", lr.catalog_path() + f"albums/{album_id}")
    if status != 204:
        raise lr._http_error(f"Deleting the album failed ({status}): {lr.parse(body)}", status)
    global _albums_cache
    _albums_cache = (0.0, None)
    lr.forget_album_count(album_id)
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
    args = sys.argv[1:]
    if len(args) >= 3 and args[:2] == ["setup", "--from"] and args[3:] in ([], ["--delete"]):
        from .credentials import run
        try:
            run(args[2], delete=len(args) == 4)
        except LrError as e:
            sys.exit(str(e))
        return
    if sys.argv[1:]:
        sys.exit(__doc__)
    mcp.run()


if __name__ == "__main__":
    main()
