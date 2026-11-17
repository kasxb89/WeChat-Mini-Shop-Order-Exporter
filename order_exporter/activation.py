"""本机一次激活与七天试用。授权到期只停用采集，不删除用户数据。"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
import hashlib
import math
import os
from pathlib import Path
import secrets
import tempfile

try:
    from ._private_config import ACTIVATION_CODE_SHA256
except ImportError:
    ACTIVATION_CODE_SHA256 = ""


@dataclass(frozen=True)
class LicenseStatus:
    state: str
    deadline: datetime | None = None
    remaining_days: int = 0

    @property
    def can_collect(self):
        return self.state in {"trial", "activated"}

    @property
    def label(self):
        return {"activated":"已激活", "expired":"试用已到期", "invalid":"请验证激活码"}.get(
            self.state, f"试用剩余 {self.remaining_days} 天")


class ActivationStore:
    def __init__(self, path: Path, clock=None):
        self.path = path
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def _save(self, value):
        self.path.parent.mkdir(parents=True,exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=".activation-",suffix=".json",dir=self.path.parent)
        try:
            with os.fdopen(fd,"w",encoding="utf-8") as stream:
                json.dump(value,stream,ensure_ascii=False)
            os.replace(temporary,self.path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def _read(self):
        if not self.path.exists():
            now = self.clock().astimezone(timezone.utc).isoformat()
            value = {"started_at":now,"last_seen_at":now,"activated_at":""}
            self._save(value)
            return value
        value = json.loads(self.path.read_text(encoding="utf-8"))
        for name in ("started_at","last_seen_at"):
            parsed = datetime.fromisoformat(value[name])
            if parsed.tzinfo is None:
                raise ValueError("授权时间格式无效")
        if value.get("activated_at"):
            datetime.fromisoformat(value["activated_at"])
        return value

    def status(self):
        try:
            value = self._read()
        except (ValueError,KeyError,TypeError):
            return LicenseStatus("invalid")
        if value.get("activated_at"):
            return LicenseStatus("activated")
        now = max(self.clock().astimezone(timezone.utc),datetime.fromisoformat(value["last_seen_at"]))
        deadline = datetime.fromisoformat(value["started_at"]) + timedelta(days=7)
        if now.isoformat() != value["last_seen_at"]:
            value["last_seen_at"] = now.isoformat()
            self._save(value)
        if now >= deadline:
            return LicenseStatus("expired",deadline)
        return LicenseStatus("trial",deadline,math.ceil((deadline-now).total_seconds()/86400))

    def activate(self, code: str) -> bool:
        digest = hashlib.sha256(code.strip().encode("utf-8")).hexdigest()
        if not secrets.compare_digest(digest, ACTIVATION_CODE_SHA256):
            return False
        try:
            value = self._read()
        except (ValueError,KeyError,TypeError):
            now = self.clock().astimezone(timezone.utc).isoformat()
            value = {"started_at":now,"last_seen_at":now}
        if not value.get("activated_at"):
            value["activated_at"] = self.clock().astimezone(timezone.utc).isoformat()
            self._save(value)
        return True
