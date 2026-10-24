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


def _client(monkeypatch):
    monkeypatch.setattr(lr, "access_token", lambda: "tok")
    monkeypatch.setattr(lr.time, "sleep", lambda s: None)


def test_get_retries_incomplete_read(monkeypatch):
    _client(monkeypatch)
    calls = []

    def urlopen(req):
        calls.append(req.get_method())
        if len(calls) < 3:
            raise lr.http.client.IncompleteRead(b"x" * 10, 90)
        resp = _Resp(b"ok")
        resp.status = 200
        return resp

    monkeypatch.setattr(lr.urllib.request, "urlopen", urlopen)
    assert lr.request("GET", "/v2/account") == (200, b"ok")
    assert calls == ["GET"] * 3


def test_get_gives_up_with_lr_error(monkeypatch):
    _client(monkeypatch)

    def urlopen(req):
        raise ConnectionResetError("reset")

    monkeypatch.setattr(lr.urllib.request, "urlopen", urlopen)
    with pytest.raises(lr.LrError, match=f"failed after {lr.GET_ATTEMPTS} attempts"):
        lr.request("GET", "/v2/account")


def test_post_is_not_retried(monkeypatch):
    _client(monkeypatch)
    calls = []

    def urlopen(req):
        calls.append(1)
        raise ConnectionResetError("reset")

    monkeypatch.setattr(lr.urllib.request, "urlopen", urlopen)
    with pytest.raises(lr.LrError):
        lr.request("POST", "/v2/x", body={})
    assert calls == [1]
