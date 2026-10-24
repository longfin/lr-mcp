"""`lr-mcp setup --from <file>`: save the credential from a JSON file downloaded from the Adobe Developer Console."""
import json
import os
import re
from pathlib import Path

from . import client as lr
from .client import LrError

KEYS = ("ADOBE_CLIENT_ID", "ADOBE_CLIENT_SECRET")


def find_credential(data):
    """Return (client_id, client_secret), preferring an OAuth Web App credential. Accepts the credential
    download ({"API_KEY", "CLIENT_SECRET", "DEF_REDIRECT_URI"}) and the project JSON (nested
    {"client_id", "client_secret"} objects)."""
    found = []

    def walk(node, key=None):
        if isinstance(node, dict):
            cid = node.get("client_id") or node.get("API_KEY")
            secret = node.get("client_secret") or node.get("CLIENT_SECRET")
            if isinstance(cid, str) and isinstance(secret, str) and cid and secret:
                found.append((key == "oauthwebapp" or "DEF_REDIRECT_URI" in node, cid, secret))
            for k, v in node.items():
                walk(v, k)
        elif isinstance(node, list):
            for v in node:
                walk(v, key)

    walk(data)
    if any(web for web, _, _ in found):
        found = [f for f in found if f[0]]
    pairs = {(cid, secret) for _, cid, secret in found}
    if not pairs:
        raise LrError("No client id and secret in this file. Download the OAuth Web App credential "
                      "from the Adobe Developer Console and retry.")
    if len(pairs) > 1:
        raise LrError(f"The file has {len(pairs)} credentials; download only the Lightroom OAuth Web App "
                      "credential (Credentials → OAuth Web App) and retry.")
    return pairs.pop()


def save_credential(cid, secret):
    """Replace the ADOBE_CLIENT_* lines in .env, keeping every other line. Returns the .env path."""
    env = lr.HOME / ".env"
    keep = []
    if env.exists():
        keep = [l for l in env.read_text().splitlines()
                if not re.match(rf"\s*({'|'.join(KEYS)})\s*=", l)]
    lines = keep + [f"ADOBE_CLIENT_ID={cid}", f"ADOBE_CLIENT_SECRET={secret}"]
    lr.HOME.mkdir(parents=True, exist_ok=True)
    tmp = env.with_name(".env.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write("\n".join(lines) + "\n")
    os.replace(tmp, env)
    env.chmod(0o600)
    return env


def run(path, delete=False):
    src = Path(path).expanduser()
    try:
        data = json.loads(src.read_text())
    except (OSError, ValueError) as e:
        raise LrError(f"Cannot read {src} as JSON: {e}")
    cid, secret = find_credential(data)
    env = save_credential(cid, secret)
    print(f"Saved client id {cid[:6]}… and its secret to {env}")
    if delete:
        src.unlink()
        print(f"Deleted {src}")
    else:
        print(f"{src} contains the client secret; delete it when you no longer need it.")
