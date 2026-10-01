"""追加式 JSONL 账本。

所有记录只追加、不改写；每条记录带账本序号与前一条记录的 SHA-256，形成可校验的
哈希链，保证任何一天的决定都能用账本内容完整重放、且事后篡改可被发现。
"""

from __future__ import annotations

import hashlib
import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

ENTRY_TYPES = {
    "assessment_recorded",
    "screening_recorded",
    "prescription_created",
    "prescription_adjusted",
    "prescription_suspended",
    "override_granted",
    "event_received",
    "manual_review",
}

GENESIS_HASH = "0" * 64


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class LedgerError(RuntimeError):
    pass


def _canonical(payload: Any) -> bytes:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def entry_hash(seq: int, ts: str, type_: str, patient_ref: str, payload: Any, prev_hash: str) -> str:
    body = _canonical({
        "seq": seq, "ts": ts, "type": type_,
        "patient_ref": patient_ref, "payload": payload, "prev_hash": prev_hash,
    })
    return hashlib.sha256(body).hexdigest()


class AppendOnlyStore:
    """按患者分区的 JSONL 追加账本。"""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._entries: list[dict] = []
        if self.path.exists():
            for line in self.path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line:
                    self._entries.append(json.loads(line))
            self._verify_chain(self._entries)

    # ------------------------------------------------------------------
    @property
    def size(self) -> int:
        return len(self._entries)

    def append(self, type_: str, patient_ref: str, payload: dict, *, ts: Optional[str] = None) -> dict:
        if type_ not in ENTRY_TYPES:
            raise LedgerError(f"未知账本记录类型 {type_}")
        if not isinstance(patient_ref, str) or not patient_ref:
            raise LedgerError("patient_ref 必填")
        if not isinstance(payload, dict):
            raise LedgerError("payload 必须是对象")
        with self._lock:
            ts = ts or utcnow_iso()
            seq = len(self._entries) + 1
            prev_hash = self._entries[-1]["hash"] if self._entries else GENESIS_HASH
            h = entry_hash(seq, ts, type_, patient_ref, payload, prev_hash)
            entry = {
                "seq": seq, "ts": ts, "type": type_,
                "patient_ref": patient_ref, "payload": payload,
                "prev_hash": prev_hash, "hash": h,
            }
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n")
            self._entries.append(entry)
            return dict(entry)

    def read(self, patient_ref: Optional[str] = None) -> list[dict]:
        if patient_ref is None:
            return [dict(e) for e in self._entries]
        return [dict(e) for e in self._entries if e["patient_ref"] == patient_ref]

    # ------------------------------------------------------------------
    @staticmethod
    def _verify_chain(entries: list[dict]) -> None:
        prev = GENESIS_HASH
        for index, e in enumerate(entries, start=1):
            expect = entry_hash(e["seq"], e["ts"], e["type"], e["patient_ref"], e["payload"], prev)
            if e["hash"] != expect:
                raise LedgerError(f"账本哈希链在 seq={e['seq']} 处不匹配，记录可能被篡改")
            if e["seq"] != index:
                raise LedgerError(f"账本序号在 seq={e['seq']} 处不连续")
            prev = e["hash"]

    def verify(self) -> dict:
        with self._lock:
            self._verify_chain(self._entries)
        return {"entries": len(self._entries), "hash_chain": "ok"}
