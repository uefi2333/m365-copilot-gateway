import base64
import json
import time

from mcg.token.jwtutil import decode_jwt_payload, is_substrate_token, seconds_remaining


def _tok(payload: dict) -> str:
    def b64(d):
        return base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()
    return f"{b64({'alg':'none'})}.{b64(payload)}.x"


def test_substrate_aud():
    t = _tok({"aud": "https://substrate.office.com/foo", "exp": int(time.time()) + 100, "oid": "a", "tid": "b"})
    c = decode_jwt_payload(t)
    assert is_substrate_token(c)
    assert seconds_remaining(c) > 0
