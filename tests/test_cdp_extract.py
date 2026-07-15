import base64
import json
import time

from mcg.token.cdp import extract_token_from_text, looks_like_substrate_url
from mcg.token.jwtutil import decode_jwt_payload


def _tok(payload: dict) -> str:
    def b64(d):
        return base64.urlsafe_b64encode(json.dumps(d, separators=(",", ":")).encode()).rstrip(b"=").decode()

    return f"{b64({'alg': 'none'})}.{b64(payload)}.sig"


def test_extract_from_ws_url():
    payload = {
        "aud": "https://substrate.office.com/ows",
        "exp": int(time.time()) + 3600,
        "oid": "oid-1",
        "tid": "tid-1",
    }
    jwt = _tok(payload)
    url = (
        "wss://substrate.office.com/m365Copilot/Chathub/oid@tid"
        f"?access_token={jwt}&ConversationId=abc"
    )
    assert looks_like_substrate_url(url)
    got = extract_token_from_text(url)
    assert got == jwt
    assert decode_jwt_payload(got)["oid"] == "oid-1"


def test_reject_wrong_aud():
    jwt = _tok({"aud": "https://graph.microsoft.com", "exp": int(time.time()) + 100})
    url = f"https://example.com?access_token={jwt}"
    assert extract_token_from_text(url) is None
