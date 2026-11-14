"""Lightroom Partner API client: Adobe IMS OAuth (Web App credential) + REST helpers.

Configuration (environment variables take precedence over the .env file):
  ADOBE_CLIENT_ID       API key of your Adobe Developer Console project (required)
  ADOBE_CLIENT_SECRET   client secret of its OAuth Web App credential (required)
  LR_MCP_PORT           local callback port (default 8000, or a free port when 8000 is busy);
                        https://localhost:<port>/callback must match the console's redirect URI pattern
  LR_MCP_HOME           state directory (default: $XDG_CONFIG_HOME/lr-mcp or ~/.config/lr-mcp)

State directory contents:
  .env          optional ADOBE_CLIENT_ID / ADOBE_CLIENT_SECRET / LR_MCP_PORT
  .tokens.json  access/refresh tokens (written by login, mode 600)
  .certs/       self-signed certificate for the HTTPS callback
"""
import base64
import http.client
import http.server
import json
import os
import random
import re
import secrets
import socket
import ssl
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path

from mcp.server.mcpserver.exceptions import ToolError


def _default_home():
    xdg = os.environ.get("XDG_CONFIG_HOME")
    return (Path(xdg) if xdg else Path.home() / ".config") / "lr-mcp"


HOME = Path(os.environ.get("LR_MCP_HOME") or _default_home()).expanduser()
TOKEN_FILE = HOME / ".tokens.json"
CERT_DIR = HOME / ".certs"

SCOPES = "AdobeID,openid,lr_partner_apis,lr_partner_rendition_apis,offline_access"
IMS = "https://ims-na1.adobelogin.com/ims"
LR = "https://lr.adobe.io"


class LrError(ToolError):
    """Subclasses ToolError so the message reaches the model instead of a generic error."""


def parse_env(text):
    out = {}
    for line in text.splitlines():
        m = re.match(r"\s*([A-Z_]+)\s*=\s*['\"]?(.*?)['\"]?\s*$", line)
        if m:
            out.setdefault(m.group(1), m.group(2))
    return out


def _load_env():
    env = HOME / ".env"
    if env.exists():
        for k, v in parse_env(env.read_text()).items():
            os.environ.setdefault(k, v)


# variables set before .env is loaded; they take precedence over anything written to .env
SHELL_ENV = {k: os.environ[k] for k in ("ADOBE_CLIENT_ID", "ADOBE_CLIENT_SECRET") if k in os.environ}


def port_config(raw):
    """(port, set, error) for an LR_MCP_PORT value; empty or missing counts as unset.
    Never raises: `setup` and `doctor` must still run to fix a bad value."""
    raw = (raw or "").strip()
    if not raw:
        return 8000, False, None
    if raw.isascii() and raw.isdigit() and 1 <= int(raw) <= 65535:
        return int(raw), True, None
    return 8000, True, f"LR_MCP_PORT must be a port number 1-65535: {raw!r}"


_load_env()
PORT, PORT_SET, PORT_ERROR = port_config(os.environ.get("LR_MCP_PORT"))


def client_id():
    v = os.environ.get("ADOBE_CLIENT_ID")
    if not v:
        raise LrError(f"ADOBE_CLIENT_ID is not set (environment or {HOME / '.env'}).")
    return v


def _client_secret():
    v = os.environ.get("ADOBE_CLIENT_SECRET")
    if not v:
        raise LrError(f"ADOBE_CLIENT_SECRET is not set (environment or {HOME / '.env'}).")
    return v


# ---------------------------------------------------------------- auth

def _listening(port):
    """`localhost` may resolve to ::1 or 127.0.0.1 in the browser, so a listener on either counts."""
    for family, addr in ((socket.AF_INET, "127.0.0.1"), (socket.AF_INET6, "::1")):
        try:
            with socket.socket(family) as s:
                s.settimeout(0.5)
                s.connect((addr, port))
                return True
        except OSError:  # nothing listening, or no IPv6 on this host
            pass
    return False


def port_free(port):
    if _listening(port):
        return False
    with socket.socket() as s:
        # like HTTPServer, so a port left in TIME_WAIT by an earlier sign-in counts as free
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("localhost", port))
        except OSError:
            return False
    return True


def _callback_server(handler):
    """Listen on PORT, or, unless LR_MCP_PORT is set, on a free port picked by the OS when PORT is
    taken; that works when the console's redirect URI pattern allows any port."""
    if PORT_ERROR:
        raise LrError(PORT_ERROR)
    for port in ([PORT] if PORT_SET else [PORT, 0]):
        if port and _listening(port):
            if PORT_SET:
                raise LrError(f"Port {port} (LR_MCP_PORT) is in use. Set LR_MCP_PORT to a free port that "
                              "the console's redirect URI pattern allows, or remove it.")
            continue
        try:
            return http.server.HTTPServer(("localhost", port), handler)
        except OSError as e:
            if PORT_SET or not port:
                raise LrError(f"Cannot listen on localhost:{port} ({e}).")
    raise LrError("Could not find a free local port for the sign-in callback.")


def _ensure_cert():
    cert, key = CERT_DIR / "localhost.pem", CERT_DIR / "localhost-key.pem"
    if not cert.exists():
        CERT_DIR.mkdir(parents=True, exist_ok=True)
        try:
            subprocess.run(
                ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                 "-keyout", str(key), "-out", str(cert), "-days", "825",
                 "-subj", "/CN=localhost", "-addext", "subjectAltName=DNS:localhost"],
                check=True, capture_output=True,
            )
        except (OSError, subprocess.CalledProcessError) as e:
            raise LrError(f"Could not create a self-signed certificate with openssl: {e}")
        key.chmod(0o600)
    return cert, key


def _token_request(params):
    basic = base64.b64encode(f"{client_id()}:{_client_secret()}".encode()).decode()
    req = urllib.request.Request(
        f"{IMS}/token/v3",
        data=urllib.parse.urlencode(params).encode(),
        headers={"Authorization": f"Basic {basic}",
                 "Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(req) as r:
            tok = json.load(r)
    except urllib.error.HTTPError as e:
        raise LrError(f"Token request failed ({e.code}): {e.read().decode()}")
    tok["obtained_at"] = int(time.time())
    if "refresh_token" not in tok and TOKEN_FILE.exists():
        # a refresh response may omit refresh_token; keep the existing one
        tok["refresh_token"] = json.loads(TOKEN_FILE.read_text()).get("refresh_token")
    HOME.mkdir(parents=True, exist_ok=True)
    TOKEN_FILE.write_text(json.dumps(tok, indent=2))
    TOKEN_FILE.chmod(0o600)
    return tok


def port_notice(port):
    return (f"Port {PORT} is busy; using port {port}. This needs the redirect URI pattern "
            f"https://localhost:[0-9]+/callback in the Adobe console. If the browser lands on "
            f"https://localhost:{PORT}/callback, stop the other app on port {PORT} or update the pattern.")


def login(timeout=300, on_url=print, on_notice=print):
    """Browser OAuth login. Returns once the callback arrives or `timeout` seconds pass.
    `on_notice` gets a message before the browser opens when sign-in falls back to another port."""
    cid = client_id()
    _client_secret()
    state = secrets.token_urlsafe(16)
    result = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            u = urllib.parse.urlparse(self.path)
            if u.path != "/callback":
                self.send_response(404)
                self.end_headers()
                return
            q = dict(urllib.parse.parse_qsl(u.query))
            result.update(q)
            ok = "code" in q and q.get("state") == state
            self.send_response(200 if ok else 400)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"Signed in. You can close this window." if ok
                             else f"Sign-in failed: {q}".encode())

        def log_message(self, *a):
            pass

    cert, key = _ensure_cert()
    srv = _callback_server(Handler)
    port = srv.server_address[1]
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(cert, key)
    srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
    srv.timeout = 1
    if port != PORT:
        on_notice(port_notice(port))

    url = f"{IMS}/authorize/v2?" + urllib.parse.urlencode({
        "client_id": cid, "scope": SCOPES, "response_type": "code",
        "redirect_uri": f"https://localhost:{port}/callback", "state": state,
    })
    on_url(url)
    webbrowser.open(url)
    deadline = time.time() + timeout
    try:
        while "code" not in result and "error" not in result and time.time() < deadline:
            try:
                srv.handle_request()
            except ssl.SSLError:
                pass  # the browser drops the connection while showing the certificate warning
    finally:
        srv.server_close()

    if "error" in result:
        raise LrError(f"Authorization failed: {result}")
    if "code" not in result:
        hint = " " + port_notice(port) if port != PORT else ""
        raise LrError(f"Sign-in was not completed within {timeout} seconds.{hint}")
    if result.get("state") != state:
        raise LrError("OAuth state mismatch.")
    return _token_request({"grant_type": "authorization_code", "code": result["code"]})


def refresh():
    if not TOKEN_FILE.exists():
        raise LrError("Not signed in. Run lr_login (or `lr-mcp login`) first.")
    rt = json.loads(TOKEN_FILE.read_text()).get("refresh_token")
    if not rt:
        raise LrError("No refresh token stored. Run lr_login (or `lr-mcp login`) again.")
    return _token_request({"grant_type": "refresh_token", "refresh_token": rt})


def access_token():
    if not TOKEN_FILE.exists():
        raise LrError("Not signed in. Run lr_login (or `lr-mcp login`) first.")
    tok = json.loads(TOKEN_FILE.read_text())
    if time.time() > tok["obtained_at"] + int(tok.get("expires_in", 0)) - 300:
        try:
            tok = refresh()
        except LrError as e:
            raise LrError(f"Token refresh failed; run lr_login (or `lr-mcp login`) again. ({e})")
    return tok["access_token"]


# ---------------------------------------------------------------- http

GET_ATTEMPTS = 6
REQUEST_TIMEOUT = 30          # seconds without data on the socket before an attempt is abandoned
RETRY_STATUSES = {429, 502, 503, 504}


def _permanent(e):
    """DNS and certificate failures: retrying cannot help, so fail at once (keeps `doctor` fast offline).
    A temporary DNS failure (EAI_AGAIN) is still retried."""
    reason = getattr(e, "reason", e)
    if isinstance(reason, socket.gaierror):
        return reason.errno != socket.EAI_AGAIN
    return isinstance(reason, ssl.SSLCertVerificationError)


def _backoff(attempt):
    """1, 3, 5, 7, 9 s with ±50% jitter so parallel workers do not retry in lockstep."""
    return (1 + 2 * attempt) * random.uniform(0.5, 1.5)


def _retry_after(headers, attempt):
    """Server-suggested delay (seconds, capped at 10 so a tool call stays within the client's timeout)."""
    v = (headers.get("Retry-After") or "").strip()
    return min(int(v), 10) if v.isascii() and v.isdigit() else _backoff(attempt)


def _too_late(deadline, delay):
    return deadline is not None and time.monotonic() + delay > deadline


def request(method, path, body=None, content_type="application/json", auth=True, headers=None, deadline=None):
    """`path` is '/v2/...' or an absolute URL. Returns (status, bytes).
    deadline: a time.monotonic() value that bounds a GET's retries: no retry is started that would
    begin after it, and an attempt waits no longer for each read than the time left (at least 1 s).
    A response that keeps arriving slowly is still read to the end."""
    headers = {"X-API-Key": client_id(), **(headers or {})}
    if auth:
        headers["Authorization"] = f"Bearer {access_token()}"
    data = None
    if body is not None:
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        headers["Content-Type"] = content_type
    url = path if path.startswith("https://") else LR + path
    req = urllib.request.Request(url, method=method, data=data, headers=headers)
    # Adobe often drops the connection mid-body on large pages when requests overlap,
    # so GETs (safe to repeat) are retried with backoff; writes get one attempt.
    attempts = GET_ATTEMPTS if method == "GET" else 1
    for attempt in range(attempts):
        last = attempt + 1 == attempts
        timeout = REQUEST_TIMEOUT
        if deadline is not None:
            timeout = min(timeout, max(deadline - time.monotonic(), 1))
        try:
            try:
                with urllib.request.urlopen(req, timeout=timeout) as r:
                    return r.status, r.read()
            except urllib.error.HTTPError as e:
                status, body = e.code, e.read()
                delay = None if last or status not in RETRY_STATUSES else _retry_after(e.headers, attempt)
                if delay is None or _too_late(deadline, delay):
                    return status, body
        except (http.client.HTTPException, urllib.error.URLError, OSError) as e:
            delay = None if last or _permanent(e) else _backoff(attempt)
            if delay is None or _too_late(deadline, delay):
                raise LrError(f"{method} {url} failed after {attempt + 1} attempt{'s' if attempt else ''}: {e!r}") from e
        time.sleep(delay)


def parse(body):
    """Decode a response body, stripping the `while (1) {}` anti-hijacking prefix."""
    text = body.decode().removeprefix("while (1) {}").strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


def get_json(path, auth=True, deadline=None):
    status, body = request("GET", path, auth=auth, deadline=deadline)
    data = parse(body)
    if status != 200:
        raise LrError(f"GET {path} returned {status}: {data}")
    return data


_catalog = None


def catalog_path():
    global _catalog
    if _catalog is None:
        _catalog = f"/v2/catalogs/{get_json('/v2/catalog')['id']}/"
    return _catalog


def _pages(path, deadline=None):
    """Yield each page's response. `links.next.href` is relative to the response's `base`.
    `deadline` bounds each request's retries (see request)."""
    url = LR + path
    while url:
        data = get_json(url, deadline=deadline)
        yield data
        nxt = data.get("links", {}).get("next", {}).get("href")
        url = urllib.parse.urljoin(data["base"], nxt) if nxt else None


def paginate(path):
    """Yield `resources` across pages."""
    for data in _pages(path):
        yield from data.get("resources", [])


COUNT_TTL = 600     # seconds a count is trusted; see album_asset_count
_count_cache = {}   # album_id -> (album `updated`, time.monotonic() when counted, count)
_count_gen = {}     # album_id -> times forget_album_count was called, so a count under way is not kept
_count_lock = threading.Lock()  # counts run in parallel, and forget_album_count may come from another tool call


def album_asset_count(album_id, updated=None, deadline=None):
    """Number of assets (photos and videos) in an album.

    The API has no count field, so this pages through the album's asset list without embedding
    the assets: one small request per 500 assets.

    updated: the album's `updated` timestamp from the albums listing. A cached count is reused
    while it is unchanged and younger than COUNT_TTL. The time limit is needed because `updated`
    does not always move when photos are added to an album.
    deadline: a time.monotonic() value. Returns None, caching nothing, when the count is not
    cached and the deadline passes before the first request or while pages are still left.
    Requests stop retrying at the deadline, so the call normally returns or raises soon after it.
    """
    hit = _count_cache.get(album_id)
    if hit and hit[0] == updated and time.monotonic() - hit[1] < COUNT_TTL:
        return hit[2]
    if deadline is not None and time.monotonic() > deadline:
        return None
    gen = _count_gen.get(album_id, 0)
    n = 0
    for data in _pages(catalog_path() + f"albums/{album_id}/assets?limit=500", deadline):
        n += len(data.get("resources", []))
        more = data.get("links", {}).get("next", {}).get("href")
        if more and deadline is not None and time.monotonic() > deadline:
            return None
    with _count_lock:
        if _count_gen.get(album_id, 0) == gen:  # else the album changed while it was being counted
            _count_cache[album_id] = (updated, time.monotonic(), n)
    return n


def forget_album_count(album_id):
    """Drop the cached count of an album whose contents were just changed, and keep a count
    of it that is under way in another thread from being cached."""
    with _count_lock:
        _count_gen[album_id] = _count_gen.get(album_id, 0) + 1
        _count_cache.pop(album_id, None)


def rendition(asset_id, kind):
    """kind: thumbnail2x | 640 | 1280 | 2048 | 2560 | fullsize.
    2560 and fullsize are generated on demand and polled until ready."""
    path = catalog_path() + f"assets/{asset_id}/renditions/{kind}"
    status, body = request("GET", path)
    if status == 404 and kind in ("2560", "fullsize"):
        gs, gb = request("POST", catalog_path() + f"assets/{asset_id}/renditions",
                         body=b"", content_type="application/octet-stream",
                         headers={"X-Generate-Renditions": kind})
        if gs != 202:
            raise LrError(f"Generating the {kind} rendition failed ({gs}): {parse(gb)}")
        for _ in range(30):
            time.sleep(2)
            status, body = request("GET", path)
            if status == 200:
                break
    if status != 200:
        raise LrError(f"Rendition {kind} for {asset_id} failed ({status}): {parse(body)}")
    return body
