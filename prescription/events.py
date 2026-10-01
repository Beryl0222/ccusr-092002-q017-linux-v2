"""事件语义校验：必填字段、乱序、缺口、迟到、未知类型。

原则（见 contracts/prescription_case.json 的 fail_closed）：
任何不合法或不可信的事件都不进入自动决策；数据缺口与乱序当日按"不安全"处理。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional

# 质量标记
MALFORMED = "MALFORMED"
UNKNOWN_TYPE = "UNKNOWN_TYPE"
MISSING_FIELD = "MISSING_FIELD"
OUT_OF_ORDER = "OUT_OF_ORDER"
GAP = "GAP"
LATE = "LATE"
DUPLICATE = "DUPLICATE"

EMERGENCY_SYMPTOMS = ("chest_pain", "syncope", "dyspnea_at_rest")
PAUSE_SYMPTOMS = ("severe_joint_pain", "acute_injury")
CAUTION_SYMPTOMS = ("fever", "unexplained_dizziness", "palpitations")
KNOWN_SYMPTOMS = EMERGENCY_SYMPTOMS + PAUSE_SYMPTOMS + CAUTION_SYMPTOMS + ("none",)

REQUIRED_STARTUP = ("symptom_check", "vitals_reading")

EVENT_REQUIRED_FIELDS: dict[str, tuple[str, ...]] = {
    "symptom_check": ("symptoms",),
    "vitals_reading": ("systolic_mmhg", "diastolic_mmhg", "heart_rate_bpm", "glucose_mmol_l"),
    "session_start": (),
    "hr_sample": ("hr_bpm",),
    "pace_sample": ("pace_kmh",),
    "symptom_onset": ("symptom",),
    "pause_request": ("reason",),
    "session_end": ("duration_min",),
}

KNOWN_EVENT_TYPES = tuple(EVENT_REQUIRED_FIELDS)


@dataclass(frozen=True)
class QualityFlag:
    code: str
    event_id: Optional[str]
    detail: str


@dataclass(frozen=True)
class Event:
    event_id: str
    patient_ref: str
    ts: datetime
    seq: int
    type: str
    data: dict[str, Any]

    @property
    def day(self) -> str:
        return self.ts.date().isoformat()


def parse_ts(value: Any) -> Optional[datetime]:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def validate_envelope(raw: Any) -> tuple[Optional[Event], list[QualityFlag]]:
    """校验单条事件信封与载荷；不合法时返回 (None, 标记)。"""
    flags: list[QualityFlag] = []
    if not isinstance(raw, dict):
        return None, [QualityFlag(MALFORMED, None, "事件不是 JSON 对象")]

    event_id = raw.get("event_id")
    ref = raw.get("patient_ref")
    ts = parse_ts(raw.get("ts"))
    seq = raw.get("seq")
    etype = raw.get("type")

    if not isinstance(event_id, str) or not event_id:
        flags.append(QualityFlag(MALFORMED, None, "缺少 event_id"))
    if not isinstance(ref, str) or not ref:
        flags.append(QualityFlag(MALFORMED, event_id, "缺少 patient_ref"))
    if ts is None:
        flags.append(QualityFlag(MALFORMED, event_id, "ts 缺失或不是 ISO8601"))
    if not isinstance(seq, int) or isinstance(seq, bool) or seq < 0:
        flags.append(QualityFlag(MALFORMED, event_id, "seq 缺失或不是非负整数"))
    if not isinstance(etype, str):
        flags.append(QualityFlag(MALFORMED, event_id, "缺少 type"))
    elif etype not in EVENT_REQUIRED_FIELDS:
        flags.append(QualityFlag(UNKNOWN_TYPE, event_id, f"未知事件类型 {etype}"))
    if flags:
        return None, flags

    data = {k: v for k, v in raw.items() if k not in {"event_id", "patient_ref", "ts", "seq", "type"}}
    for field_name in EVENT_REQUIRED_FIELDS[etype]:
        if field_name not in data or data[field_name] is None:
            flags.append(QualityFlag(MISSING_FIELD, event_id, f"{etype} 缺少字段 {field_name}"))

    # 值域与类型校验（异常值不允许静默通过）
    if etype == "symptom_check":
        symptoms = data.get("symptoms")
        if not isinstance(symptoms, list) or not symptoms:
            flags.append(QualityFlag(MALFORMED, event_id, "symptoms 必须是非空数组（无症状用 ['none']）"))
        else:
            unknown = [s for s in symptoms if s not in KNOWN_SYMPTOMS]
            if unknown:
                flags.append(QualityFlag(MALFORMED, event_id, f"未知症状 {unknown}"))
    elif etype == "vitals_reading":
        for key in ("systolic_mmhg", "diastolic_mmhg", "heart_rate_bpm", "glucose_mmol_l"):
            v = data.get(key)
            if not isinstance(v, (int, float)) or isinstance(v, bool) or v <= 0:
                flags.append(QualityFlag(MALFORMED, event_id, f"{key} 必须是正数"))
    elif etype in ("hr_sample", "pace_sample"):
        key = "hr_bpm" if etype == "hr_sample" else "pace_kmh"
        v = data.get(key)
        if not isinstance(v, (int, float)) or isinstance(v, bool) or v <= 0:
            flags.append(QualityFlag(MALFORMED, event_id, f"{key} 必须是正数"))
    elif etype == "symptom_onset":
        if data.get("symptom") not in KNOWN_SYMPTOMS or data["symptom"] == "none":
            flags.append(QualityFlag(MALFORMED, event_id, "symptom_onset 的 symptom 必须是实际症状"))
    elif etype == "session_end":
        v = data.get("duration_min")
        if not isinstance(v, (int, float)) or isinstance(v, bool) or v < 0:
            flags.append(QualityFlag(MALFORMED, event_id, "duration_min 必须是非负数"))

    if flags:
        return None, flags
    return Event(event_id=event_id, patient_ref=ref, ts=ts, seq=seq, type=etype, data=data), []


class StreamValidator:
    """按患者维护单调序号，识别乱序、缺口、迟到与重复。无状态决策逻辑之外的簿记。"""

    def __init__(self) -> None:
        self._max_seq: Optional[int] = None
        self._max_ts: Optional[datetime] = None
        self._seen_ids: set[str] = set()

    def check(self, raw: Any) -> tuple[Optional[Event], list[QualityFlag]]:
        event, flags = validate_envelope(raw)
        if event is None:
            return None, flags

        if event.event_id in self._seen_ids:
            return None, [QualityFlag(DUPLICATE, event.event_id, "重复事件，已丢弃")]

        ordering: list[QualityFlag] = []
        if self._max_seq is not None and event.seq <= self._max_seq:
            ordering.append(
                QualityFlag(OUT_OF_ORDER, event.event_id,
                            f"seq {event.seq} 不大于已见最大 seq {self._max_seq}，乱序事件不参与决策")
            )
        if self._max_seq is not None and event.seq > self._max_seq + 1:
            ordering.append(
                QualityFlag(GAP, event.event_id,
                            f"seq 从 {self._max_seq} 跳到 {event.seq}，中间事件缺失")
            )
        if self._max_ts is not None and event.ts < self._max_ts:
            ordering.append(
                QualityFlag(LATE, event.event_id,
                            f"ts {event.ts.isoformat()} 早于已处理时间 {self._max_ts.isoformat()}")
            )
        if ordering:
            # 乱序/迟到事件不参与自动决策；簿记仍要推进，避免后续事件被重复误报
            self._seen_ids.add(event.event_id)
            if not any(f.code == OUT_OF_ORDER for f in ordering):
                # GAP（及 seq 正常但时钟回拨）时推进 seq 水位；max_ts 保持较晚者
                self._max_seq = max(self._max_seq or 0, event.seq)
            return None, ordering

        self._max_seq = event.seq
        self._max_ts = event.ts
        self._seen_ids.add(event.event_id)
        return event, []
