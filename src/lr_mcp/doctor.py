"""`lr-mcp doctor`: check each setup step in order and say how to fix the first one that fails."""
import json
import os
import shutil
import socket

from . import client as lr
from .client import LrError


def _port_free(port):
    """`localhost` may resolve to ::1 or 127.0.0.1 in the browser, so a listener on either counts as busy."""
    for family, addr in ((socket.AF_INET, "127.0.0.1"), (socket.AF_INET6, "::1")):
        with socket.socket(family) as s:
            s.settimeout(0.5)
            try:
                s.connect((addr, port))
                return False
            except OSError:
                pass
    with socket.socket() as s:
        try:
            s.bind(("localhost", port))
        except OSError:
            return False
    return True


def _login_cmd():
    home = os.environ.get("LR_MCP_HOME")
    return f"LR_MCP_HOME={home} lr-mcp login" if home else "lr-mcp login"


def checks():
    """Yield (ok, message, fix). Stops after the first failure that later checks depend on."""
    yield True, f"State directory: {lr.HOME}", None

    if not lr.TOKEN_FILE.exists():
        # checked before credentials so a busy port is found before the console is configured
        if not shutil.which("openssl"):
            yield False, "openssl not found (needed for the sign-in callback certificate)", (
                "Install openssl (e.g. `brew install openssl` or your distro's package).")
            return
        if not _port_free(lr.PORT):
            yield False, f"Callback port {lr.PORT} is in use", (
                f"Pick a free port, put LR_MCP_PORT=<port> in {lr.HOME / '.env'}, and use "
                "https://localhost:<port>/callback as the redirect URI in the Adobe console.")
            return
        yield True, f"Callback port {lr.PORT} is free (redirect URI https://localhost:{lr.PORT}/callback)", None

    missing = [k for k in ("ADOBE_CLIENT_ID", "ADOBE_CLIENT_SECRET") if not os.environ.get(k)]
    if missing:
        yield False, f"Not set: {', '.join(missing)}", (
            f"[human] Create an OAuth Web App credential at https://developer.adobe.com/console (README step 1), "
            f"then put ADOBE_CLIENT_ID and ADOBE_CLIENT_SECRET in {lr.HOME / '.env'} (README step 2).")
        return
    cid = os.environ["ADOBE_CLIENT_ID"]
    yield True, f"Credentials set (client id {cid[:6] + '…' if len(cid) > 12 else 'set'})", None

    if not lr.TOKEN_FILE.exists():
        yield False, "Not signed in", (
            f"[human] Finish sign-in in the browser after running `{_login_cmd()}` (an agent may start it "
            "in the background after telling the user) and accept the localhost certificate warning.")
        return

    try:
        lr.access_token()
    except LrError as e:
        yield False, f"Token refresh failed: {e}", (
            f"[human] Run `{_login_cmd()}` again. If it keeps failing, check that the client id and "
            "secret in .env belong to the credential you signed in with.")
        return
    yield True, f"Signed in (tokens in {lr.TOKEN_FILE})", None

    status, body = lr.request("GET", "/v2/account")
    data = lr.parse(body)
    if status in (401, 403) and "api key" in json.dumps(data).lower():
        yield False, f"Lightroom API rejected the client id ({status}): {json.dumps(data)[:200]}", (
            "ADOBE_CLIENT_ID is wrong, or the tokens were issued for another credential. Fix the id "
            f"in .env (it must be the credential's Client ID / API key), then run `{_login_cmd()}` again.")
        return
    if status in (401, 403):
        yield False, f"Lightroom API refused the account ({status}): {json.dumps(data)[:200]}", (
            "[human] In the Adobe console, add the signed-in Adobe account under "
            "Lightroom Services → Manage beta users, and make sure it has a Lightroom plan or trial. "
            f"Then run `{_login_cmd()}` again.")
        return
    if status != 200:
        yield False, f"GET /v2/account returned {status}: {json.dumps(data)[:200]}", (
            "Retry later; if it persists the Lightroom API may be down.")
        return
    ent = data.get("entitlement", {})
    yield True, f"Account: {data.get('email')} (subscription: {ent.get('status')})", None
    st = ent.get("storage") or {}
    if st and st.get("used", 0) >= st.get("limit", float("inf")):
        yield True, "Note: cloud storage is full; reading works, uploads in Lightroom will not", None

    try:
        lr.catalog_path()
    except LrError as e:
        yield False, f"No catalog: {e}", (
            "[human] Open Lightroom once with this Adobe account so a catalog is created.")
        return
    yield True, "Catalog found. Setup is complete.", None


def run():
    ok_all = True
    for ok, msg, fix in checks():
        print(f"[{'ok' if ok else 'FAIL'}] {msg}")
        if fix:
            print(f"       fix: {fix}")
        ok_all = ok_all and ok
    return ok_all
