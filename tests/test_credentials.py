import json

import pytest

from lr_mcp import client as lr
from lr_mcp import credentials as setup


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


def home(tmp_path, monkeypatch):
    monkeypatch.setattr(lr, "HOME", tmp_path / "home")
    monkeypatch.setattr(lr, "SHELL_ENV", {})
    return lr.HOME


def write(tmp_path, data, name="cred.json"):
    p = tmp_path / name
    p.write_text(json.dumps(data))
    return p


def test_saved_env_round_trips(tmp_path, monkeypatch, capsys):
    home(tmp_path, monkeypatch)
    setup.run(str(write(tmp_path, {"API_KEY": "abc123", "CLIENT_SECRET": "p8e-x_y.z~1", "DEF_REDIRECT_URI": "u"})))
    env = lr.parse_env((lr.HOME / ".env").read_text())
    assert env["ADOBE_CLIENT_ID"] == "abc123" and env["ADOBE_CLIENT_SECRET"] == "p8e-x_y.z~1"


def test_rejects_unexpected_characters():
    with pytest.raises(lr.LrError, match="unexpected characters") as e:
        setup.find_credential({"API_KEY": "abc\nLR_MCP_PORT=1", "CLIENT_SECRET": "s3cret"})
    assert "s3cret" not in str(e.value)


def test_delete_symlinked_source_removes_target(tmp_path, monkeypatch, capsys):
    home(tmp_path, monkeypatch)
    target = write(tmp_path, project(webapp("cid", "sec")), "real.json")
    link = tmp_path / "link.json"
    link.symlink_to(target)
    setup.run(str(link), delete=True)
    assert not target.exists() and not link.is_symlink()


def test_symlinked_env_updates_target(tmp_path, monkeypatch, capsys):
    h = home(tmp_path, monkeypatch)
    h.mkdir()
    target = tmp_path / "dotfiles.env"
    target.write_text("LR_MCP_PORT=9000\n")
    (h / ".env").symlink_to(target)
    setup.run(str(write(tmp_path, project(webapp("cid", "sec")))))
    assert (h / ".env").is_symlink()
    assert target.read_text() == "LR_MCP_PORT=9000\nADOBE_CLIENT_ID=cid\nADOBE_CLIENT_SECRET=sec\n"
    assert not list(tmp_path.glob(".env.*"))


def test_warns_when_shell_env_overrides(tmp_path, monkeypatch, capsys):
    home(tmp_path, monkeypatch)
    monkeypatch.setattr(lr, "SHELL_ENV", {"ADOBE_CLIENT_ID": "old"})
    setup.run(str(write(tmp_path, project(webapp("cid", "sec")))))
    assert "ADOBE_CLIENT_ID is set in the environment" in capsys.readouterr().out


def test_delete_failure_is_reported(tmp_path, monkeypatch, capsys):
    home(tmp_path, monkeypatch)
    src = write(tmp_path, project(webapp("cid", "sec")))
    monkeypatch.setattr(type(src), "unlink", lambda self, *a, **k: (_ for _ in ()).throw(PermissionError(13, "Permission denied")))
    with pytest.raises(lr.LrError, match="Saved the credential, but could not delete"):
        setup.run(str(src), delete=True)
    assert (lr.HOME / ".env").exists()


def test_cli_dispatch(monkeypatch):
    import sys
    from lr_mcp import server
    calls = []
    monkeypatch.setattr(setup, "run", lambda path, delete=False: calls.append((path, delete)))
    for argv in (["setup", "--from", "f.json"], ["setup", "--from", "f.json", "--delete"]):
        monkeypatch.setattr(sys, "argv", ["lr-mcp", *argv])
        server.main()
    assert calls == [("f.json", False), ("f.json", True)]
    monkeypatch.setattr(sys, "argv", ["lr-mcp", "setup", "--delete", "--from", "f.json"])
    with pytest.raises(SystemExit):
        server.main()
