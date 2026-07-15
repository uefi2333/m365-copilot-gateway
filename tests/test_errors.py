from mcg.api.errors import map_runtime, map_substrate


def test_no_account():
    e = map_runtime(RuntimeError("no active accounts with valid substrate tokens"))
    assert e.status_code == 503
    assert e.detail["code"] == "no_account"
    assert "hint" in e.detail


def test_token():
    e = map_runtime(RuntimeError("token expired, refresh failed"))
    assert e.status_code == 401
    assert e.detail["code"] == "token_invalid"


def test_rate():
    e = map_substrate(RuntimeError("throttled 429"))
    assert e.status_code == 429
    assert e.detail["code"] == "rate_limited"
