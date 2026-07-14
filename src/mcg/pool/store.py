from __future__ import annotations

import json
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

from mcg.token.fabric import TokenFabric
from mcg.token.jwtutil import decode_jwt_payload, is_substrate_token, seconds_remaining

AccountStatus = Literal["active", "cooldown", "disabled", "expired"]


@dataclass
class Account:
    id: str
    label: str
    status: AccountStatus = "active"
    token: str = ""
    profile_path: str = ""
    errors: int = 0
    last_used: float = 0.0
    last_success: float = 0.0
    cooldown_until: float = 0.0
    meta: dict[str, Any] = field(default_factory=dict)

    def public_dict(self, fabric: TokenFabric | None = None) -> dict[str, Any]:
        rem = 0
        valid = False
        if self.token:
            try:
                claims = decode_jwt_payload(self.token)
                valid = is_substrate_token(claims) and seconds_remaining(claims) > 0
                rem = seconds_remaining(claims)
            except Exception:  # noqa: BLE001
                valid = False
        return {
            "id": self.id,
            "label": self.label,
            "status": self.status,
            "errors": self.errors,
            "last_used": self.last_used,
            "last_success": self.last_success,
            "cooldown_until": self.cooldown_until,
            "token_valid": valid,
            "token_ttl_sec": rem,
            "has_token": bool(self.token),
            "meta": self.meta,
        }


class AccountPool:
    def __init__(
        self,
        data_dir: Path,
        fabric: TokenFabric,
        *,
        strategy: str = "round_robin",
        cooldown_sec: int = 60,
        max_consecutive_errors: int = 3,
    ) -> None:
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.data_dir / "accounts.json"
        self.fabric = fabric
        self.strategy = strategy
        self.cooldown_sec = cooldown_sec
        self.max_consecutive_errors = max_consecutive_errors
        self._rr = 0
        self.accounts: dict[str, Account] = {}
        self.load()

    def load(self) -> None:
        if not self.path.exists():
            self.accounts = {}
            return
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        self.accounts = {}
        for item in raw.get("accounts", []):
            acc = Account(**{k: item[k] for k in Account.__dataclass_fields__ if k in item})
            self.accounts[acc.id] = acc
            if acc.token:
                self.fabric.put_hot(acc.id, acc.token)

    def save(self) -> None:
        payload = {"accounts": [asdict(a) for a in self.accounts.values()]}
        self.path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        try:
            self.path.chmod(0o600)
        except OSError:
            pass

    def list_public(self) -> list[dict[str, Any]]:
        return [a.public_dict(self.fabric) for a in self.accounts.values()]

    def import_token(self, token: str, label: str = "") -> Account:
        claims = decode_jwt_payload(token)
        if not is_substrate_token(claims):
            raise ValueError("not a substrate.office.com token")
        if seconds_remaining(claims) <= 0:
            raise ValueError("token expired")
        acc_id = str(claims.get("oid") or uuid.uuid4())
        label = label or f"user-{acc_id[:8]}"
        acc = self.accounts.get(acc_id) or Account(id=acc_id, label=label)
        acc.label = label
        acc.token = token
        acc.status = "active"
        acc.errors = 0
        acc.meta = {"tid": claims.get("tid"), "upn": claims.get("upn") or claims.get("unique_name")}
        self.accounts[acc_id] = acc
        self.fabric.put_hot(acc_id, token)
        self.save()
        return acc

    def delete(self, account_id: str) -> bool:
        if account_id not in self.accounts:
            return False
        del self.accounts[account_id]
        self.save()
        return True

    def set_status(self, account_id: str, status: AccountStatus) -> Account:
        acc = self.accounts[account_id]
        acc.status = status
        self.save()
        return acc

    def _eligible(self) -> list[Account]:
        now = time.time()
        out: list[Account] = []
        for acc in self.accounts.values():
            if acc.status == "disabled":
                continue
            if acc.cooldown_until > now:
                continue
            if not acc.token:
                continue
            try:
                claims = decode_jwt_payload(acc.token)
                if not is_substrate_token(claims) or seconds_remaining(claims) <= 0:
                    acc.status = "expired"
                    continue
            except Exception:  # noqa: BLE001
                continue
            if acc.status == "expired":
                acc.status = "active"
            out.append(acc)
        return out

    def acquire(self, sticky_key: str | None = None) -> Account:
        eligible = self._eligible()
        if not eligible:
            raise RuntimeError("no active accounts with valid substrate tokens")
        if self.strategy == "sticky" and sticky_key:
            for acc in eligible:
                if acc.meta.get("sticky") == sticky_key or acc.id == sticky_key:
                    acc.last_used = time.time()
                    return acc
        if self.strategy == "least_load":
            eligible.sort(key=lambda a: (a.errors, a.last_used))
            acc = eligible[0]
        else:
            self._rr %= len(eligible)
            acc = eligible[self._rr]
            self._rr += 1
        acc.last_used = time.time()
        return acc

    def mark_success(self, account_id: str) -> None:
        acc = self.accounts[account_id]
        acc.errors = 0
        acc.last_success = time.time()
        acc.status = "active"
        self.save()

    def mark_error(self, account_id: str, cooldown: bool = False) -> None:
        acc = self.accounts[account_id]
        acc.errors += 1
        if cooldown or acc.errors >= self.max_consecutive_errors:
            acc.status = "cooldown"
            acc.cooldown_until = time.time() + self.cooldown_sec
        self.save()
