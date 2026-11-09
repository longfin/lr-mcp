import email.message
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
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass


def test_refresh_keeps_existing_refresh_token(monkeypatch):
    lr.TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    lr.TOKEN_FILE.write_text(json.dumps({"access_token": "old", "refresh_token": "rt",
                                         "expires_in": 86400, "obtained_at": 0}))
    sent = {}

    def urlopen(req, timeout=None):
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
    monkeypatch.setattr(lr.urllib.request, "urlopen", lambda req, timeout=None: pytest.fail("should not refresh"))
    assert lr.access_token() == "fresh"


def _client(monkeypatch):
    monkeypatch.setattr(lr, "access_token", lambda: "tok")
    monkeypatch.setattr(lr.time, "sleep", lambda s: None)


def test_get_retries_incomplete_read(monkeypatch):
    _client(monkeypatch)
    calls = []

    def urlopen(req, timeout=None):
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

    def urlopen(req, timeout=None):
        raise ConnectionResetError("reset")

    monkeypatch.setattr(lr.urllib.request, "urlopen", urlopen)
    with pytest.raises(lr.LrError, match=f"failed after {lr.GET_ATTEMPTS} attempts"):
        lr.request("GET", "/v2/account")


def test_post_is_not_retried(monkeypatch):
    _client(monkeypatch)
    calls = []

    def urlopen(req, timeout=None):
        calls.append(1)
        raise ConnectionResetError("reset")

    monkeypatch.setattr(lr.urllib.request, "urlopen", urlopen)
    with pytest.raises(lr.LrError):
        lr.request("POST", "/v2/x", body={})
    assert calls == [1]


class _Cut(_Resp):
    """A response whose body read is cut short, like Adobe dropping a large page mid-transfer."""

    def read(self, *a):
        raise lr.http.client.IncompleteRead(b"x" * 10, 90)


def test_get_retries_body_cut_mid_read(monkeypatch):
    _client(monkeypatch)
    slept, resps = [], [_Cut(b""), _Cut(b""), _Resp(b"ok")]
    monkeypatch.setattr(lr.time, "sleep", slept.append)
    monkeypatch.setattr(lr.urllib.request, "urlopen", lambda req, timeout=None: resps.pop(0))
    assert lr.request("GET", "/v2/account") == (200, b"ok")
    assert slept == [1, 3]


def test_get_backoff_schedule_and_timeout(monkeypatch):
    _client(monkeypatch)
    slept, timeouts = [], []
    monkeypatch.setattr(lr.time, "sleep", slept.append)

    def urlopen(req, timeout=None):
        timeouts.append(timeout)
        raise lr.http.client.RemoteDisconnected("closed")

    monkeypatch.setattr(lr.urllib.request, "urlopen", urlopen)
    with pytest.raises(lr.LrError, match="failed after 6 attempts"):
        lr.request("GET", "/v2/account")
    assert slept == [1, 3, 5, 7, 9]
    assert timeouts == [lr.REQUEST_TIMEOUT] * 6


def _make_http_error(status, headers=None):
    msg = email.message.Message()
    for k, v in (headers or {}).items():
        msg[k] = v
    return lr.urllib.error.HTTPError("https://x", status, "err", msg, io.BytesIO(b"err"))


def test_get_retries_503_honouring_retry_after(monkeypatch):
    _client(monkeypatch)
    slept, calls = [], []
    monkeypatch.setattr(lr.time, "sleep", slept.append)

    def urlopen(req, timeout=None):
        calls.append(1)
        if len(calls) == 1:
            raise _make_http_error(503, {"Retry-After": "7"})
        if len(calls) == 2:
            raise _make_http_error(429)
        return _Resp(b"ok")

    monkeypatch.setattr(lr.urllib.request, "urlopen", urlopen)
    assert lr.request("GET", "/v2/account") == (200, b"ok")
    assert slept == [7, 3]


def test_get_returns_other_http_errors_without_retry(monkeypatch):
    _client(monkeypatch)
    calls = []

    def urlopen(req, timeout=None):
        calls.append(1)
        raise _make_http_error(404)

    monkeypatch.setattr(lr.urllib.request, "urlopen", urlopen)
    assert lr.request("GET", "/v2/account") == (404, b"err")
    assert calls == [1]


def test_get_fails_fast_on_dns_error(monkeypatch):
    _client(monkeypatch)
    slept = []
    monkeypatch.setattr(lr.time, "sleep", slept.append)

    def urlopen(req, timeout=None):
        raise lr.urllib.error.URLError(lr.socket.gaierror(8, "nodename nor servname provided"))

    monkeypatch.setattr(lr.urllib.request, "urlopen", urlopen)
    with pytest.raises(lr.LrError, match="failed after 1 attempt:"):
        lr.request("GET", "/v2/account")
    assert slept == []
