"""只追加的 JSONL 事件账本。

每个病例一个 <case_id>.jsonl 文件，每行一个完整事件信封。
- seq 为文件内追加序号；
- 重放排序按 (event_time, event_id)，与接收顺序无关；
- late 标记相对接收顺序计算（event_time 早于此前已收到事件的最大 event_time）；
- event_id 幂等：重复投递返回已存在事件而不是再次写入。
"""

import json
import threading
from pathlib import Path

from . import timeutil


class DuplicateEvent(Exception):
    def __init__(self, event_id: str):
        super().__init__(f"事件已存在: {event_id}")
        self.event_id = event_id


class EventStore:
    def __init__(self, directory: str | Path):
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)
        self._locks = {}
        self._guard = threading.Lock()

    def _lock_for(self, case_id: str) -> threading.Lock:
        with self._guard:
            if case_id not in self._locks:
                self._locks[case_id] = threading.Lock()
            return self._locks[case_id]

    def _path(self, case_id: str) -> Path:
        if "/" in case_id or ".." in case_id or not case_id:
            raise ValueError("非法病例 id")
        return self.dir / f"{case_id}.jsonl"

    def list_cases(self) -> list:
        return sorted(p.stem for p in self.dir.glob("*.jsonl"))

    def append(self, case_id: str, event: dict) -> dict:
        """加锁追加；调用方需先解析好时间与 event_id。"""
        path = self._path(case_id)
        lock = self._lock_for(case_id)
        with lock:
            existing = self._load_locked(path)
            if any(e["event_id"] == event["event_id"] for e in existing):
                raise DuplicateEvent(event["event_id"])
            event = dict(event)
            event["seq"] = len(existing) + 1
            event.setdefault("case_id", case_id)
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")
                fh.flush()
            return event

    @staticmethod
    def _load_locked(path: Path) -> list:
        if not path.exists():
            return []
        events = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                events.append(json.loads(line))
        return events

    def load_raw(self, case_id: str) -> list:
        """按接收顺序（seq）返回事件，时间解析为 datetime。"""
        path = self._path(case_id)
        with self._lock_for(case_id):
            events = self._load_locked(path)
        out = []
        for e in events:
            e = dict(e)
            e["event_time"] = timeutil.parse(e["event_time"])
            e["ingested_at"] = timeutil.parse(e.get("ingested_at") or e["event_time"])
            out.append(e)
        return out

    def load_for_replay(self, case_id: str) -> list:
        """按 (event_time, event_id) 排序，并标注 late（晚到/乱序）。"""
        raw = self.load_raw(case_id)
        max_seen = None
        late_flags = {}
        for e in sorted(raw, key=lambda x: x["seq"]):
            # late 只描述外部信号的乱序；服务内部派生的 monitor_finding
            # 使用发现发生时间入库，不参与晚到判定。
            if e["event_type"] == "monitor_finding":
                late_flags[e["event_id"]] = False
                continue
            if max_seen is not None and e["event_time"] < max_seen:
                late_flags[e["event_id"]] = True
            else:
                late_flags.setdefault(e["event_id"], False)
            if max_seen is None or e["event_time"] > max_seen:
                max_seen = e["event_time"]
        ordered = sorted(raw, key=lambda x: (x["event_time"], x["event_id"]))
        for e in ordered:
            e["late"] = late_flags.get(e["event_id"], False)
        return ordered
