"""Lightroom Partner API client: Adobe IMS OAuth (Web App credential) + REST helpers.

Configuration (environment variables take precedence over the .env file):
  ADOBE_CLIENT_ID       API key of your Adobe Developer Console project (required)
  ADOBE_CLIENT_SECRET   client secret of its OAuth Web App credential (required)
  LR_MCP_PORT           local callback port (default 8000); the redirect URI
                        https://localhost:<port>/callback must be allowed in the console
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
import re
import secrets
import ssl
import subprocess
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


def _load_env():
    env = HOME / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            m = re.match(r"\s*([A-Z_]+)\s*=\s*['\"]?(.*?)['\"]?\s*$", line)
            if m:
                os.environ.setdefault(m.group(1), m.group(2))


_load_env()
PORT = int(os.environ.get("LR_MCP_PORT", "8000"))
REDIRECT_URI = f"https://localhost:{PORT}/callback"


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


def login(timeout=300, on_url=print):
    """Browser OAuth login. Returns once the callback arrives or `timeout` seconds pass."""
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
    try:
        srv = http.server.HTTPServer(("localhost", PORT), Handler)
    except OSError as e:
        raise LrError(f"Cannot listen on localhost:{PORT} ({e}). Set LR_MCP_PORT to a free port "
                      f"and allow https://localhost:<port>/callback in the Adobe console.")
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(cert, key)
    srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
    srv.timeout = 1

    url = f"{IMS}/authorize/v2?" + urllib.parse.urlencode({
        "client_id": cid, "scope": SCOPES, "response_type": "code",
        "redirect_uri": REDIRECT_URI, "state": state,
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
        raise LrError(f"Sign-in was not completed within {timeout} seconds.")
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


def request(method, path, body=None, content_type="application/json", auth=True, headers=None):
    """`path` is '/v2/...' or an absolute URL. Returns (status, bytes)."""
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
    # so GETs (safe to repeat) are retried with backoff.
    attempts = GET_ATTEMPTS if method == "GET" else 1
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()
        except (http.client.HTTPException, urllib.error.URLError, OSError) as e:
            if attempt + 1 == attempts:
                raise LrError(f"{method} {url} failed after {attempts} attempts: {e!r}") from e
            time.sleep(1 + 2 * attempt)


def parse(body):
    """Decode a response body, stripping the `while (1) {}` anti-hijacking prefix."""
    text = body.decode().removeprefix("while (1) {}").strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


def get_json(path, auth=True):
    status, body = request("GET", path, auth=auth)
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


def paginate(path):
    """Yield `resources` across pages. `links.next.href` is relative to the response's `base`."""
    url = LR + path
    while url:
        data = get_json(url)
        yield from data.get("resources", [])
        nxt = data.get("links", {}).get("next", {}).get("href")
        url = urllib.parse.urljoin(data["base"], nxt) if nxt else None


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
