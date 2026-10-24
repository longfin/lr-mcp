import json

from lr_mcp import client as lr
from lr_mcp import doctor


def results(monkeypatch, status=200, body=None, signed_in=True):
    if signed_in:
        lr.TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
        lr.TOKEN_FILE.write_text("{}")
    elif lr.TOKEN_FILE.exists():
        lr.TOKEN_FILE.unlink()
    monkeypatch.setattr(lr, "access_token", lambda: "tok")
    monkeypatch.setattr(lr, "catalog_path", lambda: "/v2/catalogs/c/")
    payload = body if body is not None else {"email": "a@b.c", "entitlement": {"status": "subscriber"}}
    monkeypatch.setattr(lr, "request", lambda *a, **k: (status, b"while (1) {}" + json.dumps(payload).encode()))
    return list(doctor.checks())


def test_all_ok(monkeypatch):
    out = results(monkeypatch)
    assert all(ok for ok, _, _ in out)
    assert out[-1][1].startswith("Catalog found")


def test_missing_credentials(monkeypatch):
    monkeypatch.delenv("ADOBE_CLIENT_SECRET")
    monkeypatch.setattr(lr, "port_free", lambda port: True)
    ok, msg, fix = results(monkeypatch, signed_in=False)[-1]
    assert not ok and "ADOBE_CLIENT_SECRET" in msg and fix.startswith("[human]")


def test_not_signed_in(monkeypatch):
    monkeypatch.setattr(lr, "port_free", lambda port: True)
    ok, msg, fix = results(monkeypatch, signed_in=False)[-1]
    assert not ok and msg == "Not signed in" and "lr-mcp login" in fix


def test_set_port_in_use_is_reported_before_credentials(monkeypatch):
    monkeypatch.delenv("ADOBE_CLIENT_ID")
    monkeypatch.setattr(lr, "port_free", lambda port: False)
    monkeypatch.setattr(lr, "PORT_SET", True)
    out = results(monkeypatch, signed_in=False)
    ok, msg, fix = out[-1]
    assert not ok and "in use" in msg and "LR_MCP_PORT" in fix
    assert not any("ADOBE_CLIENT_ID" in m for _, m, _ in out)


def test_default_port_in_use_is_not_fatal(monkeypatch):
    monkeypatch.setattr(lr, "port_free", lambda port: False)
    monkeypatch.setattr(lr, "PORT_SET", False)
    out = results(monkeypatch, signed_in=False)
    assert any(ok and "pick a free port" in m for ok, m, _ in out)
    assert out[-1][1] == "Not signed in"


def test_missing_credentials_suggests_setup(monkeypatch):
    monkeypatch.delenv("ADOBE_CLIENT_ID")
    monkeypatch.setattr(lr, "port_free", lambda port: True)
    ok, msg, fix = results(monkeypatch, signed_in=False)[-1]
    assert not ok and "setup --from" in fix


def test_wrong_api_key(monkeypatch):
    ok, msg, fix = results(monkeypatch, 403, {"code": "403003", "description": "Api Key is invalid"})[-1]
    assert not ok and "ADOBE_CLIENT_ID" in fix


def test_not_a_beta_user(monkeypatch):
    ok, msg, fix = results(monkeypatch, 403, {"code": "1003", "description": "Access is forbidden"})[-1]
    assert not ok and "Manage beta users" in fix


def test_port_busy_on_ipv6_only():
    import socket
    with socket.socket(socket.AF_INET6) as s:
        s.bind(("::1", 0))
        s.listen()
        assert not lr.port_free(s.getsockname()[1])


def test_login_hint_includes_custom_home(monkeypatch):
    monkeypatch.setattr(lr, "port_free", lambda port: True)
    ok, msg, fix = results(monkeypatch, signed_in=False)[-1]
    assert f"LR_MCP_HOME={lr.HOME}" in fix or str(lr.HOME) in fix
