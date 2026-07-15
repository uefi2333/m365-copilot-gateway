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
        max_consecutive_errors: int = 8,
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

    def bind_profile(self, account_id: str, profile_path: str) -> Account:
        acc = self.accounts[account_id]
        acc.profile_path = profile_path
        self.save()
        return acc

    def refresh_token(self, account_id: str, token: str) -> Account:
        acc = self.accounts[account_id]
        st = self.fabric.put_hot(account_id, token)
        if not st.valid:
            raise ValueError(st.error or "invalid token")
        acc.token = token
        acc.status = "active"
        acc.errors = 0
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
        dirty = False
        for acc in self.accounts.values():
            if acc.status == "disabled":
                continue
            # auto-exit cooldown when timer elapsed
            if acc.status == "cooldown" and acc.cooldown_until <= now:
                acc.status = "active"
                acc.cooldown_until = 0.0
                acc.errors = 0
                dirty = True
            if acc.cooldown_until > now:
                continue
            if not acc.token:
                continue
            try:
                claims = decode_jwt_payload(acc.token)
                if not is_substrate_token(claims) or seconds_remaining(claims) <= 0:
                    acc.status = "expired"
                    dirty = True
                    continue
            except Exception:  # noqa: BLE001
                continue
            if acc.status in ("expired", "cooldown"):
                acc.status = "active"
                dirty = True
            out.append(acc)
        if dirty:
            try:
                self.save()
            except Exception:  # noqa: BLE001
                pass
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
        """Count failure. Cooldown only after max_consecutive_errors.

        `cooldown=True` is kept for call-site compatibility but no longer
        forces an immediate single-error ban (kills single-account pools).
        """
        acc = self.accounts[account_id]
        acc.errors += 1
        if acc.errors >= self.max_consecutive_errors:
            acc.status = "cooldown"
            # shorter ban if caller marked soft-ish path via cooldown flag
            sec = self.cooldown_sec if cooldown else self.cooldown_sec
            acc.cooldown_until = time.time() + sec
        else:
            # stay active so the next request can retry
            if acc.status == "cooldown" and acc.cooldown_until <= time.time():
                acc.status = "active"
                acc.cooldown_until = 0.0
        self.save()

    def mark_soft_error(self, account_id: str) -> None:
        """Transient upstream flake — half weight toward cooldown."""
        acc = self.accounts[account_id]
        # every other soft error counts as one hard error unit
        acc.meta["_soft"] = int(acc.meta.get("_soft") or 0) + 1
        if acc.meta["_soft"] % 2 == 0:
            self.mark_error(account_id, cooldown=False)
        else:
            self.save()
