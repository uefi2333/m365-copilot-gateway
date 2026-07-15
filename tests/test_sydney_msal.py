from pathlib import Path

from mcg.token.sydney_msal import DEFAULT_CLIENT_ID, SYDNEY_SCOPES, SydneyMsal
from mcg.token.jwtutil import is_substrate_token


def test_defaults_match_cramt_lezi():
    assert DEFAULT_CLIENT_ID == "c0ab8ce9-e9a0-42e7-b064-33d422df41f1"
    assert "sydney/M365Chat.Read" in SYDNEY_SCOPES[0] or any(
        "M365Chat.Read" in s for s in SYDNEY_SCOPES
    )
    assert any("sydney.readwrite" in s for s in SYDNEY_SCOPES)


def test_pkce_start_writes_pending(tmp_path: Path):
    sm = SydneyMsal(tmp_path, account_key="t1")
    start = sm.start_pkce()
    assert "client_id=" in start.auth_url
    assert DEFAULT_CLIENT_ID in start.auth_url
    assert "code_challenge" in start.auth_url
    assert "M365Chat.Read" in start.auth_url or "sydney" in start.auth_url
    pending = tmp_path / "msal" / "pkce_pending.json"
    assert pending.exists()
    assert "code_verifier" in pending.read_text()


def test_aud_prefix_accepts_sydney():
    claims = {"aud": "https://substrate.office.com/sydney", "exp": 9999999999}
    assert is_substrate_token(claims)


def test_fabric_defaults_sydney(tmp_path: Path):
    from mcg.token.fabric import TokenFabric

    f = TokenFabric(tmp_path)
    assert f.use_sydney_msal is True
    assert f.msal_client_id == DEFAULT_CLIENT_ID
