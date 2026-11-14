import email.message
import io
import json
import re
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
    monkeypatch.setattr(lr, "get_json", lambda url, auth=True, deadline=None: pages[url])
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
    monkeypatch.setattr(lr, "PORT_ERROR", "LR_MCP_PORT must be a port number 1-65535: 'x'")
    with pytest.raises(lr.LrError, match="1-65535"):
        lr._callback_server(_Ok)


def test_parse_env():
    assert lr.parse_env("A=1\n# c\nB = '2'\nA=3\nlower=x") == {"A": "1", "B": "2"}


def test_port_config():
    assert lr.port_config(None) == (8000, False, None)
    assert lr.port_config("") == (8000, False, None)
    assert lr.port_config("  ") == (8000, False, None)
    assert lr.port_config(" 8765 ") == (8765, True, None)
    for bad in ("x", "-1", "0", "70000", "1_000", "0x1F", "٨٠٠٠"):
        port, port_set, err = lr.port_config(bad)
        assert (port, port_set) == (8000, True) and "1-65535" in err, bad


class _NoTls:
    def __init__(self, *a):
        pass

    def load_cert_chain(self, *a):
        pass

    def wrap_socket(self, sock, **k):
        return sock


@pytest.fixture
def no_tls(monkeypatch):
    monkeypatch.setattr(lr, "_ensure_cert", lambda: ("cert", "key"))
    monkeypatch.setattr(lr.ssl, "SSLContext", _NoTls)


def test_login_announces_port_fallback_before_opening_browser(monkeypatch, no_tls):
    events = []

    monkeypatch.setattr(lr.webbrowser, "open", lambda url: events.append(("browser", url)))
    with _busy_port() as busy:
        monkeypatch.setattr(lr, "PORT", busy.getsockname()[1])
        monkeypatch.setattr(lr, "PORT_SET", False)
        with pytest.raises(lr.LrError, match=r"not completed.*localhost:\[0-9\]\+/callback"):
            lr.login(timeout=0, on_url=lambda u: events.append(("url", u)),
                     on_notice=lambda m: events.append(("notice", m)))
    assert [e for e, _ in events] == ["notice", "url", "browser"]
    notice = events[0][1]
    assert f"Port {lr.PORT} is busy" in notice and "https://localhost:[0-9]+/callback" in notice
    used = int(re.search(r"using port (\d+)", notice).group(1))
    assert used != lr.PORT and f"localhost%3A{used}%2Fcallback" in events[1][1]


def test_login_is_quiet_on_default_port(monkeypatch, no_tls):
    monkeypatch.setattr(lr.webbrowser, "open", lambda url: None)
    import socket
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    monkeypatch.setattr(lr, "PORT", port)
    monkeypatch.setattr(lr, "PORT_SET", False)
    notices = []
    with pytest.raises(lr.LrError, match=r"seconds\.$"):
        lr.login(timeout=0, on_url=lambda u: None, on_notice=notices.append)
    assert notices == []


def _client(monkeypatch):
    monkeypatch.setattr(lr, "access_token", lambda: "tok")
    monkeypatch.setattr(lr.time, "sleep", lambda s: None)
    monkeypatch.setattr(lr.random, "uniform", lambda a, b: 1.0)  # no jitter, so sleeps are predictable


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


def test_get_retries_when_error_body_is_cut(monkeypatch):
    _client(monkeypatch)
    slept, calls = [], []
    monkeypatch.setattr(lr.time, "sleep", slept.append)

    def urlopen(req, timeout=None):
        calls.append(1)
        if len(calls) == 1:
            raise lr.urllib.error.HTTPError("https://x", 500, "err", email.message.Message(), _Cut(b""))
        return _Resp(b"ok")

    monkeypatch.setattr(lr.urllib.request, "urlopen", urlopen)
    assert lr.request("GET", "/v2/account") == (200, b"ok")
    assert slept == [1]


def test_backoff_has_jitter():
    assert all(0.5 <= lr._backoff(0) <= 1.5 for _ in range(50))
    assert 4.5 <= lr._backoff(4) <= 13.5


def test_retry_after_is_capped_and_ignores_odd_values():
    def hdr(v):
        msg = email.message.Message()
        if v is not None:
            msg["Retry-After"] = v
        return msg

    assert lr._retry_after(hdr("7"), 0) == 7
    assert lr._retry_after(hdr("120"), 0) == 10
    for odd in ("²", "Wed, 21 Oct 2026 07:28:00 GMT", "-5", "", None):
        assert 0.5 <= lr._retry_after(hdr(odd), 0) <= 1.5  # falls back to the first backoff step


def test_temporary_dns_failure_is_retried(monkeypatch):
    _client(monkeypatch)
    calls = []

    def urlopen(req, timeout=None):
        calls.append(1)
        if len(calls) == 1:
            raise lr.urllib.error.URLError(lr.socket.gaierror(lr.socket.EAI_AGAIN, "try again"))
        return _Resp(b"ok")

    monkeypatch.setattr(lr.urllib.request, "urlopen", urlopen)
    assert lr.request("GET", "/v2/account") == (200, b"ok")
    assert calls == [1, 1]


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
    assert slept == [7, 3]  # Retry-After, then plain backoff for the 429 without one


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


deadlines = []  # the deadline each page request of album_pages was given


def album_pages(monkeypatch, sizes):
    """Serve album `a` as pages of the given sizes; returns the list of fetched URLs."""
    base = lr.LR + "/v2/catalogs/c/"
    fetched = []
    deadlines.clear()

    def get_json(url, auth=True, deadline=None):
        fetched.append(url)
        deadlines.append(deadline)
        i = int(url.partition("&page=")[2] or 0)
        page = {"base": base, "resources": [{"id": f"{i}-{k}"} for k in range(sizes[i])]}
        if i + 1 < len(sizes):
            page["links"] = {"next": {"href": f"albums/a/assets?limit=500&page={i + 1}"}}
        return page

    monkeypatch.setattr(lr, "catalog_path", lambda: "/v2/catalogs/c/")
    monkeypatch.setattr(lr, "get_json", get_json)
    monkeypatch.setattr(lr, "_count_cache", {})
    monkeypatch.setattr(lr, "_count_gen", {})
    return fetched


def test_album_asset_count_adds_up_pages_without_embedding_assets(monkeypatch):
    fetched = album_pages(monkeypatch, [500, 500, 37])
    assert lr.album_asset_count("a") == 1037
    assert fetched[0] == lr.LR + "/v2/catalogs/c/albums/a/assets?limit=500"
    assert len(fetched) == 3 and not any("embed" in u for u in fetched)


def test_album_asset_count_of_empty_album(monkeypatch):
    album_pages(monkeypatch, [0])
    assert lr.album_asset_count("a") == 0


def test_album_asset_count_is_cached_until_updated_changes(monkeypatch):
    fetched = album_pages(monkeypatch, [3])
    assert lr.album_asset_count("a", "2024-01-01") == 3
    assert lr.album_asset_count("a", "2024-01-01") == 3
    assert len(fetched) == 1
    assert lr.album_asset_count("a", "2024-02-02") == 3
    assert len(fetched) == 2


def test_album_asset_count_cache_expires(monkeypatch):
    fetched = album_pages(monkeypatch, [3])
    now = [1000.0]
    monkeypatch.setattr(lr.time, "monotonic", lambda: now[0])
    lr.album_asset_count("a", "2024-01-01")
    now[0] += lr.COUNT_TTL - 1
    lr.album_asset_count("a", "2024-01-01")
    assert len(fetched) == 1
    now[0] += 2
    lr.album_asset_count("a", "2024-01-01")
    assert len(fetched) == 2


def slow_pages(monkeypatch, fetched):
    """Make every fetched page take 10 s on a fake clock that starts at 0."""
    monkeypatch.setattr(lr.time, "monotonic", lambda: 10.0 * len(fetched))


def test_album_asset_count_gives_up_at_deadline_with_pages_left(monkeypatch):
    fetched = album_pages(monkeypatch, [500, 500, 37])
    slow_pages(monkeypatch, fetched)
    assert lr.album_asset_count("a", "u", deadline=5) is None
    assert len(fetched) == 1
    assert deadlines == [5]  # the requests are held to the deadline too
    assert lr._count_cache == {}  # a partial count must not be cached
    assert lr.album_asset_count("a", "u") == 1037


def test_album_asset_count_keeps_a_count_finished_past_the_deadline(monkeypatch):
    fetched = album_pages(monkeypatch, [37])
    slow_pages(monkeypatch, fetched)
    assert lr.album_asset_count("a", "u", deadline=5) == 37  # the last page arrived late, but it is complete


def test_forget_album_count(monkeypatch):
    fetched = album_pages(monkeypatch, [3])
    lr.album_asset_count("a", "u")
    lr.forget_album_count("a")
    lr.forget_album_count("never-counted")
    lr.album_asset_count("a", "u")
    assert len(fetched) == 2


def test_album_asset_count_past_the_deadline_still_answers_from_the_cache(monkeypatch):
    fetched = album_pages(monkeypatch, [37])
    assert lr.album_asset_count("a", "u") == 37
    assert lr.album_asset_count("a", "u", deadline=time.monotonic() - 1) == 37
    assert lr.album_asset_count("a", "changed", deadline=time.monotonic() - 1) is None
    assert len(fetched) == 1  # no request is started past the deadline


def test_album_changed_while_being_counted_is_not_cached(monkeypatch):
    fetched = album_pages(monkeypatch, [3])
    get_json = lr.get_json

    def changed_meanwhile(url, auth=True, deadline=None):
        lr.forget_album_count("a")  # e.g. lr_add_to_project_album finishing in another thread
        return get_json(url, auth, deadline)

    monkeypatch.setattr(lr, "get_json", changed_meanwhile)
    assert lr.album_asset_count("a", "u") == 3  # the caller still gets what was counted
    assert "a" not in lr._count_cache
    monkeypatch.setattr(lr, "get_json", get_json)
    assert lr.album_asset_count("a", "u") == 3
    assert len(fetched) == 2 and "a" in lr._count_cache


def _clock(monkeypatch, start=100.0):
    """A fake clock that only time.sleep moves; returns the list of sleeps."""
    now, slept = [start], []

    def sleep(s):
        slept.append(s)
        now[0] += s

    monkeypatch.setattr(lr.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(lr.time, "sleep", sleep)
    return slept


def test_get_does_not_retry_past_deadline(monkeypatch):
    _client(monkeypatch)
    slept, timeouts = _clock(monkeypatch), []

    def urlopen(req, timeout=None):
        timeouts.append(timeout)
        raise lr.http.client.RemoteDisconnected("closed")

    monkeypatch.setattr(lr.urllib.request, "urlopen", urlopen)
    with pytest.raises(lr.LrError, match="failed after 3 attempts"):
        lr.request("GET", "/v2/account", deadline=105)  # 5 s left: room for the 1 s and 3 s waits, not the 5 s one
    assert slept == [1, 3]
    assert timeouts == [5, 4, 1]  # each attempt waits no longer than the time left


def test_get_past_deadline_gets_one_short_attempt(monkeypatch):
    _client(monkeypatch)
    slept, timeouts = _clock(monkeypatch), []

    def urlopen(req, timeout=None):
        timeouts.append(timeout)
        raise lr.http.client.RemoteDisconnected("closed")

    monkeypatch.setattr(lr.urllib.request, "urlopen", urlopen)
    with pytest.raises(lr.LrError, match="failed after 1 attempt:"):
        lr.request("GET", "/v2/account", deadline=50)
    assert slept == [] and timeouts == [1]


def test_get_returns_retryable_status_instead_of_waiting_past_deadline(monkeypatch):
    _client(monkeypatch)
    slept, calls = _clock(monkeypatch), []

    def urlopen(req, timeout=None):
        calls.append(1)
        raise _make_http_error(429, {"Retry-After": "10"})

    monkeypatch.setattr(lr.urllib.request, "urlopen", urlopen)
    assert lr.request("GET", "/v2/account", deadline=105) == (429, b"err")
    assert slept == [] and calls == [1]


def test_get_without_deadline_is_unchanged_by_a_far_deadline(monkeypatch):
    _client(monkeypatch)
    slept, timeouts = _clock(monkeypatch), []

    def urlopen(req, timeout=None):
        timeouts.append(timeout)
        raise lr.http.client.RemoteDisconnected("closed")

    monkeypatch.setattr(lr.urllib.request, "urlopen", urlopen)
    with pytest.raises(lr.LrError, match="failed after 6 attempts"):
        lr.request("GET", "/v2/account", deadline=100 + 3600)
    assert slept == [1, 3, 5, 7, 9] and timeouts == [lr.REQUEST_TIMEOUT] * 6


def _renditions(monkeypatch, replies):
    """Answer request() from `replies` (method -> list of (status, body)); returns the calls made."""
    calls = []

    def request(method, path, **kw):
        calls.append(method)
        return replies[method].pop(0) if len(replies[method]) > 1 else replies[method][0]

    monkeypatch.setattr(lr, "catalog_path", lambda: "/v2/catalogs/c/")
    monkeypatch.setattr(lr, "request", request)
    monkeypatch.setattr(lr.time, "sleep", lambda s: None)
    return calls


@pytest.mark.parametrize("kind, replies, retryable", [
    ("2048", {"GET": [(404, b'{"code": 1002}')]}, False),               # no such asset
    ("2048", {"GET": [(403, b"")]}, False),
    ("2048", {"GET": [(503, b"")]}, True),
    ("2048", {"GET": [(500, b"")]}, True),
    ("fullsize", {"GET": [(404, b"")], "POST": [(202, b"")]}, True),     # still generating after the polling
    ("fullsize", {"GET": [(404, b"")], "POST": [(403, b"")]}, False),
    ("fullsize", {"GET": [(404, b"")], "POST": [(429, b"")]}, True),
])
def test_rendition_errors_say_whether_to_retry(monkeypatch, kind, replies, retryable):
    _renditions(monkeypatch, replies)
    with pytest.raises(lr.LrError) as e:
        lr.rendition("a1", kind)
    assert e.value.retryable is retryable


def test_rendition_polls_until_generated(monkeypatch):
    calls = _renditions(monkeypatch, {"GET": [(404, b""), (404, b""), (200, b"jpeg")], "POST": [(202, b"")]})
    assert lr.rendition("a1", "2560") == b"jpeg"
    assert calls == ["GET", "POST", "GET", "GET"]


def test_network_failures_say_whether_to_retry(monkeypatch):
    _client(monkeypatch)
    monkeypatch.setattr(lr.urllib.request, "urlopen", lambda req, timeout=None: (_ for _ in ()).throw(
        ConnectionResetError("reset")))
    with pytest.raises(lr.LrError) as e:
        lr.request("GET", "/v2/account")
    assert e.value.retryable is True
    monkeypatch.setattr(lr.urllib.request, "urlopen", lambda req, timeout=None: (_ for _ in ()).throw(
        lr.urllib.error.URLError(lr.socket.gaierror(8, "nodename nor servname provided"))))
    with pytest.raises(lr.LrError) as e:
        lr.request("GET", "/v2/account")
    assert e.value.retryable is False
