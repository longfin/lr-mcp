import io
import json
import time

import pytest

from lr_mcp import client as lr


def test_parse_strips_prefix():
    assert lr.parse(b'while (1) {}\n{"a": 1}') == {"a": 1}
    assert lr.parse(b"while (1) {}") is None
    assert lr.parse(b"not json") == "not json"


def test_paginate_follows_relative_next_links(monkeypatch):
    pages = {
        lr.LR + "/v2/catalogs/c/assets?limit=2": {
            "base": lr.LR + "/v2/catalogs/c/",
            "resources": [1, 2],
            "links": {"next": {"href": "assets?limit=2&page=2"}},
        },
        lr.LR + "/v2/catalogs/c/assets?limit=2&page=2": {
            "base": lr.LR + "/v2/catalogs/c/",
            "resources": [3],
        },
    }
    monkeypatch.setattr(lr, "get_json", lambda url, auth=True: pages[url])
    assert list(lr.paginate("/v2/catalogs/c/assets?limit=2")) == [1, 2, 3]


def test_get_json_raises_lr_error(monkeypatch):
    monkeypatch.setattr(lr, "request", lambda *a, **k: (403, b'while (1) {}{"code": 1003}'))
    with pytest.raises(lr.LrError, match="403"):
        lr.get_json("/v2/account")


def test_missing_client_id(monkeypatch):
    monkeypatch.delenv("ADOBE_CLIENT_ID")
    with pytest.raises(lr.LrError, match="ADOBE_CLIENT_ID"):
        lr.client_id()


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass


def test_refresh_keeps_existing_refresh_token(monkeypatch):
    lr.TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    lr.TOKEN_FILE.write_text(json.dumps({"access_token": "old", "refresh_token": "rt",
                                         "expires_in": 86400, "obtained_at": 0}))
    sent = {}

    def urlopen(req):
        sent["body"] = req.data.decode()
        return _Resp(json.dumps({"access_token": "new", "expires_in": 86400}).encode())

    monkeypatch.setattr(lr.urllib.request, "urlopen", urlopen)
    assert lr.access_token() == "new"
    assert "grant_type=refresh_token" in sent["body"]
    saved = json.loads(lr.TOKEN_FILE.read_text())
    assert saved["refresh_token"] == "rt"
    assert lr.TOKEN_FILE.stat().st_mode & 0o777 == 0o600


def test_access_token_does_not_refresh_when_fresh(monkeypatch):
    lr.TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    lr.TOKEN_FILE.write_text(json.dumps({"access_token": "fresh", "refresh_token": "rt",
                                         "expires_in": 86400, "obtained_at": int(time.time())}))
    monkeypatch.setattr(lr.urllib.request, "urlopen", lambda req: pytest.fail("should not refresh"))
    assert lr.access_token() == "fresh"


class _Ok(__import__("http.server").server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()

    def log_message(self, *a):
        pass


def test_port_free_after_time_wait():
    import http.server
    import threading
    import urllib.request
    srv = http.server.HTTPServer(("localhost", 0), _Ok)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.handle_request)
    t.start()
    urllib.request.urlopen(f"http://127.0.0.1:{port}/").read()
    t.join()
    srv.server_close()
    assert lr.port_free(port)


def test_listening_without_ipv6(monkeypatch):
    import socket
    real = socket.socket

    def fake(family=socket.AF_INET, *a, **k):
        if family == socket.AF_INET6:
            raise OSError(97, "Address family not supported by protocol")
        return real(family, *a, **k)

    monkeypatch.setattr(socket, "socket", fake)
    with real() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    assert not lr._listening(port)


def _busy_port():
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    s.listen()
    return s


def test_callback_server_falls_back_when_default_is_busy(monkeypatch):
    with _busy_port() as busy:
        monkeypatch.setattr(lr, "PORT", busy.getsockname()[1])
        monkeypatch.setattr(lr, "PORT_SET", False)
        srv = lr._callback_server(_Ok)
        try:
            assert srv.server_address[1] not in (0, lr.PORT)
        finally:
            srv.server_close()


def test_callback_server_keeps_explicit_port(monkeypatch):
    with _busy_port() as busy:
        monkeypatch.setattr(lr, "PORT", busy.getsockname()[1])
        monkeypatch.setattr(lr, "PORT_SET", True)
        with pytest.raises(lr.LrError, match="LR_MCP_PORT"):
            lr._callback_server(_Ok)


def test_callback_server_reports_bad_port(monkeypatch):
    monkeypatch.setattr(lr, "PORT_ERROR", "LR_MCP_PORT is not a number: 'x'")
    with pytest.raises(lr.LrError, match="not a number"):
        lr._callback_server(_Ok)


def test_parse_env():
    assert lr.parse_env("A=1\n# c\nB = '2'\nA=3\nlower=x") == {"A": "1", "B": "2"}
