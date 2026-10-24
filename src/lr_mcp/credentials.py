"""`lr-mcp setup --from <file>`: save the credential from a JSON file downloaded from the Adobe Developer Console."""
import json
import os
import re
import tempfile
from pathlib import Path

from . import client as lr
from .client import LrError

KEYS = ("ADOBE_CLIENT_ID", "ADOBE_CLIENT_SECRET")
# what Adobe issues; anything else (newlines, quotes, spaces) would not survive .env parsing
VALUE = re.compile(r"[A-Za-z0-9._~-]+")


def find_credential(data):
    """Return (client_id, client_secret), preferring an OAuth Web App credential. Accepts the credential
    download ({"API_KEY", "CLIENT_SECRET", "DEF_REDIRECT_URI"}) and the project JSON (nested
    {"client_id", "client_secret"} objects)."""
    found = []

    def walk(node, key=None):
        if isinstance(node, dict):
            cid = node.get("client_id") or node.get("API_KEY")
            secret = node.get("client_secret") or node.get("CLIENT_SECRET")
            if isinstance(cid, str) and isinstance(secret, str) and cid.strip() and secret.strip():
                cid, secret = cid.strip(), secret.strip()
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
    cid, secret = pairs.pop()
    for name, v in (("client id", cid), ("client secret", secret)):
        if not VALUE.fullmatch(v):
            raise LrError(f"The {name} in this file has unexpected characters; download the credential again.")
    return cid, secret


def save_credential(cid, secret):
    """Replace the ADOBE_CLIENT_* lines in .env, keeping every other line. Returns the .env path.
    A symlinked .env is updated in place of its target."""
    env = (lr.HOME / ".env").resolve()
    try:
        keep = []
        if env.exists():
            keep = [l for l in env.read_text().splitlines()
                    if not re.match(rf"\s*({'|'.join(KEYS)})\s*=", l)]
        lines = keep + [f"ADOBE_CLIENT_ID={cid}", f"ADOBE_CLIENT_SECRET={secret}"]
        env.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=env.parent, prefix=".env.")  # O_EXCL, mode 600
        try:
            with os.fdopen(fd, "w") as f:
                f.write("\n".join(lines) + "\n")
            os.replace(tmp, env)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
    except OSError as e:
        raise LrError(f"Could not update {env}: {e.strerror or e}")
    env.chmod(0o600)
    return env


def run(path, delete=False):
    src = Path(path).expanduser()
    real = src.resolve()
    try:
        data = json.loads(real.read_text())
    except (OSError, ValueError) as e:
        raise LrError(f"Cannot read {src} as JSON: {e}")
    cid, secret = find_credential(data)
    env = save_credential(cid, secret)
    print(f"Saved client id {cid[:6]}… and its secret to {env}")
    for key, new in (("ADOBE_CLIENT_ID", cid), ("ADOBE_CLIENT_SECRET", secret)):
        if lr.SHELL_ENV.get(key, new) != new:
            print(f"Warning: {key} is set in the environment and overrides .env; unset it.")
    if delete:
        try:
            real.unlink()
            if src.is_symlink():
                src.unlink()
        except OSError as e:
            raise LrError(f"Saved the credential, but could not delete {real}: {e.strerror or e}. "
                          "Delete it yourself; it contains the client secret.")
        print(f"Deleted {real}")
    else:
        print(f"{real} contains the client secret; delete it when you no longer need it.")
