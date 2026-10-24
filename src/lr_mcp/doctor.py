"""`lr-mcp doctor`: check each setup step in order and say how to fix the first one that fails."""
import json
import os
import shutil

from . import client as lr
from .client import LrError

def _cmd(args):
    home = os.environ.get("LR_MCP_HOME")
    return f"LR_MCP_HOME={home} lr-mcp {args}" if home else f"lr-mcp {args}"

def _login_cmd():
    return _cmd("login")

def checks():
    """Yield (ok, message, fix). Stops after the first failure that later checks depend on."""
    yield True, f"State directory: {lr.HOME}", None

    if not lr.TOKEN_FILE.exists():
        # checked before credentials so a busy port is found before the console is configured
        if not shutil.which("openssl"):
            yield False, "openssl not found (needed for the sign-in callback certificate)", (
                "Install openssl (e.g. `brew install openssl` or your distro's package).")
            return
        if lr.port_free(lr.PORT):
            yield True, f"Callback port {lr.PORT} is free (redirect URI https://localhost:{lr.PORT}/callback)", None
        elif lr.PORT_SET:
            yield False, f"Callback port {lr.PORT} (LR_MCP_PORT) is in use", (
                f"Change LR_MCP_PORT in {lr.HOME / '.env'} to a free port, or remove it so sign-in "
                "picks one.")
            return
        else:
            yield True, (f"Callback port {lr.PORT} is in use; sign-in will pick a free port, which needs "
                         "the redirect URI pattern https://localhost:[0-9]+/callback in the Adobe console"), None

    missing = [k for k in ("ADOBE_CLIENT_ID", "ADOBE_CLIENT_SECRET") if not os.environ.get(k)]
    if missing:
        yield False, f"Not set: {', '.join(missing)}", (
            f"[human] Create an OAuth Web App credential at https://developer.adobe.com/console and download "
            f"it as JSON (README step 1), then run `{_cmd('setup --from <file>.json --delete')}` "
            "(README step 2).")
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
