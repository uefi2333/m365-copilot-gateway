from mcg.token.jwtutil import decode_jwt_payload, is_substrate_token, seconds_remaining
import base64, json, time


def _tok(aud="https://substrate.office.com/sydney", exp_delta=3600):
    h = base64.urlsafe_b64encode(b'{"alg":"none"}').decode().rstrip("=")
    p = base64.urlsafe_b64encode(
        json.dumps({"aud": aud, "exp": int(time.time()) + exp_delta, "oid": "x"}).encode()
    ).decode().rstrip("=")
    return f"{h}.{p}.sig"


def test_seconds_remaining_accepts_token_string():
    t = _tok()
    assert seconds_remaining(t) > 0
    assert is_substrate_token(t)


def test_seed_sidecar_and_status(tmp_path):
    from mcg.token.fabric import TokenFabric
    from mcg.token.sydney_msal import SydneyMsal

    f = TokenFabric(tmp_path, use_sydney_msal=True)
    f.put_refresh_token("acc1", "rt-demo-value")
    side = tmp_path / "msal" / "acc1.rt.json"
    assert side.exists()
    data = json.loads(side.read_text())
    assert data["refresh_token"] == "rt-demo-value"
    sm = SydneyMsal(tmp_path, account_key="acc1")
    sm.seed_sidecar_rt("rt-2")
    assert json.loads(side.read_text())["refresh_token"] == "rt-2"


def test_prompt_rekey(tmp_path):
    from mcg.token.fabric import TokenFabric

    f = TokenFabric(tmp_path)
    m = tmp_path / "msal"
    m.mkdir()
    (m / "pending.json").write_text('{"k":1}')
    (m / "pending.rt.json").write_text('{"refresh_token":"abc"}')
    f.put_refresh_token("pending", "abc")
    f.rekey_msal_artifacts("pending", "oid1")
    assert (m / "oid1.json").exists()
    assert (m / "oid1.rt.json").exists()
    assert f.get_refresh_token("oid1") == "abc"
