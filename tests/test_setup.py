import json

import pytest

from lr_mcp import client as lr
from lr_mcp import setup


def project(*creds):
    return {"project": {"workspace": {"details": {"credentials": list(creds)}}}}


def webapp(cid, secret):
    return {"integration_type": "oauthwebapp", "oauthwebapp": {"client_id": cid, "client_secret": secret}}


def test_finds_oauth_web_app_credential():
    data = project({"jwt": {"client_id": "other", "client_secret": "x"}}, webapp("cid", "sec"))
    assert setup.find_credential(data) == ("cid", "sec")


def test_finds_credential_download():
    data = {"CLIENT_SECRET": "sec", "API_KEY": "cid", "DEF_REDIRECT_URI": "https://localhost:8000/callback"}
    assert setup.find_credential(data) == ("cid", "sec")


def test_no_credential():
    with pytest.raises(lr.LrError, match="No client id"):
        setup.find_credential(project({"oauthwebapp": {"client_id": "cid"}}))


def test_ambiguous_credentials():
    with pytest.raises(lr.LrError, match="2 credentials"):
        setup.find_credential(project(webapp("a", "1"), webapp("b", "2")))


def test_run_replaces_credentials_and_keeps_other_lines(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(lr, "HOME", tmp_path / "home")
    lr.HOME.mkdir()
    env = lr.HOME / ".env"
    env.write_text("ADOBE_CLIENT_ID=old\nLR_MCP_PORT=8765\nADOBE_CLIENT_SECRET = old")
    src = tmp_path / "project.json"
    src.write_text(json.dumps(project(webapp("abcdefghijkl", "s3cret"))))

    setup.run(str(src), delete=True)

    assert env.read_text() == "LR_MCP_PORT=8765\nADOBE_CLIENT_ID=abcdefghijkl\nADOBE_CLIENT_SECRET=s3cret\n"
    assert env.stat().st_mode & 0o777 == 0o600
    assert not src.exists()
    out = capsys.readouterr().out
    assert "abcdef…" in out and "s3cret" not in out


def test_run_keeps_source_without_delete(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(lr, "HOME", tmp_path / "home")
    src = tmp_path / "project.json"
    src.write_text(json.dumps(project(webapp("cid", "sec"))))
    setup.run(str(src))
    assert src.exists() and "delete it" in capsys.readouterr().out
