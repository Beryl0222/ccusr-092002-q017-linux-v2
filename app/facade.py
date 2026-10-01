"""服务门面：所有用例都通过只追加账本落盘，任何结论可由事件流重放。"""

import copy
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo

from . import monitor, timeutil
from .dosing import build_plan
from .eligibility import evaluate_eligibility
from .ledger import DuplicateEvent, EventStore
from .registry import RuleRegistry

ALLOWED_AUTHORITIES = {
    "attending_physician",
    "sports_medicine_physician",
    "cardiologist",
    "community_physician",
}

SIGNAL_EVENT_TYPES = {
    "symptom_reported",
    "device_reading",
    "session_started",
    "session_ended",
    "review_resolved",
}
REVIEW_RESOLUTIONS = {"resume", "keep_suspended", "adjust_plan"}

# 人工调整的结构护栏（超出范围请走正式规则修订，而不是在单次处方里突破）。
ADJUSTMENT_BOUNDS = {
    "pace_kmh": (2.0, 12.0),
    "minutes": (5, 120),
    "weekly_sessions": (1, 7),
    "warmup_minutes": (0, 30),
    "cooldown_minutes": (0, 30),
}


class ServiceError(ValueError):
    def __init__(self, message: str, code: str = "invalid_request", status: int = 400):
        super().__init__(message)
        self.code = code
        self.status = status


class PrescriptionService:
    def __init__(self, store: EventStore, registry: RuleRegistry | None = None,
                 default_timezone: str = "Asia/Shanghai"):
        self.store = store
        self.registry = registry or RuleRegistry()
        self.default_tz = ZoneInfo(default_timezone)

    # ------------------------------------------------------------------ 工具

    @staticmethod
    def _new_event_id() -> str:
        return f"evt_{uuid.uuid4().hex[:16]}"

    def _require_case(self, case_id: str) -> list:
        if case_id not in self.store.list_cases():
            raise ServiceError(f"病例不存在: {case_id}", "not_found", 404)
        return self.store.load_for_replay(case_id)

    def _case_opened(self, ordered: list) -> dict:
        for ev in ordered:
            if ev["event_type"] == "case_opened":
                return ev
        raise ServiceError("病例缺少开案事件", "illegal_state", 409)

    def _case_tz(self, opened: dict):
        name = (opened.get("payload") or {}).get("timezone")
        try:
            return ZoneInfo(name) if name else self.default_tz
        except Exception:
            return self.default_tz

    def _append(self, case_id, event_type, payload, actor_ref, at=None,
                event_id=None, ingested_at=None) -> dict:
        event_time = timeutil.parse(at) if at else timeutil.now()
        event = {
            "event_id": event_id or self._new_event_id(),
            "case_id": case_id,
            "event_type": event_type,
            "event_time": timeutil.to_iso(event_time),
            "ingested_at": timeutil.to_iso(timeutil.parse(ingested_at) if ingested_at else timeutil.now()),
            "actor_ref": actor_ref,
            "payload": payload,
        }
        try:
            return self.store.append(case_id, event)
        except DuplicateEvent as exc:
            raise ServiceError(str(exc), "duplicate_event", 409) from exc

    def _active_overrides(self, ordered: list, at) -> dict:
        out = {}
        for ev in ordered:
            if ev["event_type"] != "override_granted" or ev["event_time"] > at:
                continue
            p = ev["payload"]
            valid_from = timeutil.parse(p.get("valid_from") or ev["event_time"])
            valid_until = timeutil.parse(p["valid_until"])
            if valid_from <= at < valid_until:
                out[p["rule_code"]] = dict(p, _event_id=ev["event_id"])
        return out

    def _fold(self, ordered: list, as_of=None):
        monitor_rules = self.registry.latest("monitoring")
        return monitor.fold(ordered, self.registry, monitor_rules, as_of=as_of)

    def _serialize_event(self, ev: dict) -> dict:
        e = {k: v for k, v in ev.items() if k != "late"}
        if isinstance(e.get("event_time"), object) and not isinstance(e["event_time"], str):
            e["event_time"] = timeutil.to_iso(ev["event_time"])
            e["ingested_at"] = timeutil.to_iso(ev["ingested_at"])
        e["late"] = ev.get("late", False)
        return e

    # ------------------------------------------------------------------ 开案

    def open_case(self, patient_ref: str, assessment: dict, actor_ref: str,
                  case_id: str | None = None, event_id: str | None = None,
                  at: str | None = None, timezone_name: str | None = None) -> dict:
        if not patient_ref or not isinstance(assessment, dict):
            raise ServiceError("patient_ref 与 assessment 必填")
        case_id = case_id or f"case-{uuid.uuid4().hex[:12]}"
        if case_id in self.store.list_cases():
            raise ServiceError(f"病例已存在: {case_id}", "case_exists", 409)
        payload = {"patient_ref": patient_ref, "assessment": copy.deepcopy(assessment)}
        if timezone_name:
            payload["timezone"] = timezone_name
        return self._append(case_id, "case_opened", payload, actor_ref, at=at, event_id=event_id)

    # ------------------------------------------------------------------ 裁决

    def decide(self, case_id: str, actor_ref: str, event_id: str | None = None,
               at: str | None = None) -> dict:
        ordered = self._require_case(case_id)
        opened = self._case_opened(ordered)
        decision_time = timeutil.parse(at) if at else timeutil.now()
        assessment = opened["payload"]["assessment"]

        ci = self.registry.latest("contraindications")
        dosing = self.registry.latest("dosing")
        versions = {
            "contraindications": ci["rules_version"],
            "dosing": dosing["rules_version"],
            "monitoring": self.registry.latest("monitoring")["rules_version"],
        }
        overrides = self._active_overrides(ordered, decision_time)
        eligibility = evaluate_eligibility(assessment, ci, overrides)

        plan_snapshot = None
        if eligibility["outcome"] == "eligible":
            plan = build_plan(assessment, dosing, eligibility["dose_modifiers"])
            plan["physician_supervised_start"] = eligibility["requires_physician_supervision"]
            if eligibility["requires_physician_supervision"]:
                plan["safety_notes"].insert(
                    0, "该处方基于医生对禁忌规则的覆盖开具，首次运动须在医务人员监督下进行。"
                )
            plan_snapshot = {
                "plan": plan,
                "rules_versions": versions,
                "overridden_codes": eligibility["overridden_codes"],
            }

        payload = {
            "outcome": eligibility["outcome"],
            "rules_version": ci["rules_version"],
            "rules_versions": versions,
            "plan_snapshot": plan_snapshot,
            "plan": plan_snapshot["plan"] if plan_snapshot else None,
            "basis_codes": sorted(
                eligibility["absolute_hits"]
                + eligibility["review_hits"]
                + eligibility["relative_hits"]
                + [f"OVERRIDE:{c}" for c in eligibility["overridden_codes"]]
            ),
            "eligibility_detail": eligibility,
        }
        return self._append(case_id, "prescription_decided", payload, actor_ref,
                            at=at, event_id=event_id)

    # ------------------------------------------------------------------ 覆盖

    def grant_override(self, case_id: str, rule_code: str, reason: str, authority: str,
                       valid_until: str, actor_ref: str, valid_from: str | None = None,
                       event_id: str | None = None, at: str | None = None) -> dict:
        ordered = self._require_case(case_id)
        if not reason or not reason.strip():
            raise ServiceError("覆盖规则必须写明理由 reason")
        if authority not in ALLOWED_AUTHORITIES:
            raise ServiceError(f"未知或无权限角色: {authority}；允许: {sorted(ALLOWED_AUTHORITIES)}")
        ci = self.registry.latest("contraindications")
        known = {r["code"] for r in ci["absolute"] + ci["review"] + ci["relative"]}
        if rule_code not in known:
            raise ServiceError(f"未知规则码: {rule_code}")
        decision_time = timeutil.parse(at) if at else timeutil.now()
        start = timeutil.parse(valid_from) if valid_from else decision_time
        end = timeutil.parse(valid_until)
        if end <= start:
            raise ServiceError("valid_until 必须晚于生效时间")

        payload = {
            "rule_code": rule_code,
            "reason": reason.strip(),
            "authority": authority,
            "valid_from": timeutil.to_iso(start),
            "valid_until": timeutil.to_iso(end),
        }
        return self._append(case_id, "override_granted", payload, actor_ref,
                            at=at, event_id=event_id)

    # ------------------------------------------------------------------ 调整

    def adjust_plan(self, case_id: str, changes: dict, reason: str, actor_ref: str,
                    event_id: str | None = None, at: str | None = None) -> dict:
        ordered = self._require_case(case_id)
        if not reason or not reason.strip():
            raise ServiceError("调整计划必须写明理由 reason")
        adjust_time = timeutil.parse(at) if at else timeutil.now()
        state = self._fold(ordered, as_of=adjust_time)
        if state.active_plan is None:
            raise ServiceError("当前没有生效处方，无法调整；请先完成裁决", "no_plan", 409)
        before_snapshot = {
            "plan": copy.deepcopy(state.active_plan["plan"]),
            "rules_versions": state.active_plan.get("rules_versions", {}),
            "overridden_codes": sorted(state.used_override_codes),
        }
        new_plan = copy.deepcopy(before_snapshot["plan"])
        self._apply_changes(new_plan, changes)
        after_snapshot = {
            "plan": new_plan,
            "rules_versions": before_snapshot["rules_versions"],
            "overridden_codes": before_snapshot["overridden_codes"],
        }
        payload = {
            "before": before_snapshot,
            "after": after_snapshot,
            "reason": reason.strip(),
            "changed_fields": sorted(changes.keys()),
        }
        return self._append(case_id, "plan_adjusted", payload, actor_ref,
                            at=at, event_id=event_id)

    @staticmethod
    def _apply_changes(plan: dict, changes: dict):
        allowed = set(ADJUSTMENT_BOUNDS)
        unknown = set(changes) - allowed
        if unknown:
            raise ServiceError(f"不允许调整的字段: {sorted(unknown)}（心率区间由规则计算）")
        for key, val in changes.items():
            lo_b, hi_b = ADJUSTMENT_BOUNDS[key]
            if key == "weekly_sessions" and isinstance(val, (int, float)):
                val = [int(val), int(val)]
            if not (isinstance(val, list) and len(val) == 2):
                raise ServiceError(f"{key} 必须是 [下限, 上限] 两个数值")
            lo, hi = val
            if not all(isinstance(x, (int, float)) for x in (lo, hi)) or lo > hi:
                raise ServiceError(f"{key} 必须为数值且下限<=上限")
            if not (lo_b <= lo and hi <= hi_b):
                raise ServiceError(f"{key} 超出结构护栏 [{lo_b}, {hi_b}]，请走规则修订流程")
            plan[key] = [lo, hi] if key != "weekly_sessions" else [int(lo), int(hi)]

    # ------------------------------------------------------------------ 复评

    def reassess(self, case_id: str, actor_ref: str, reason: str | None = None,
                 event_id: str | None = None, at: str | None = None) -> dict:
        ordered = self._require_case(case_id)
        opened = self._case_opened(ordered)
        assess_time = timeutil.parse(at) if at else timeutil.now()
        ci = self.registry.latest("contraindications")
        overrides = self._active_overrides(ordered, assess_time)
        result = evaluate_eligibility(opened["payload"]["assessment"], ci, overrides)

        if result["outcome"] == "eligible":
            return {"outcome": "eligible", "event": None, "detail": result}

        if result["outcome"] == "contraindicated":
            trigger = result["absolute_hits"]
            guidance = ("当前评估命中运动禁忌，处方暂停；请先到医疗机构完成评估，"
                        "不要自行运动。")
        else:
            trigger = result["review_hits"]
            guidance = "关键信息缺失或待复核，处方暂停；补全信息并经医务人员复核后再恢复。"
        payload = {
            "trigger_codes": trigger,
            "review_required": True,
            "guidance": guidance,
            "reason": reason,
            "eligibility_detail": result,
            "rules_version": ci["rules_version"],
        }
        event = self._append(case_id, "prescription_paused", payload, actor_ref,
                             at=at, event_id=event_id)
        return {"outcome": result["outcome"], "event": event, "detail": result}

    def resume(self, case_id: str, note: str, actor_ref: str,
               event_id: str | None = None, at: str | None = None) -> dict:
        self._require_case(case_id)
        if not note or not note.strip():
            raise ServiceError("恢复处方必须写明医生理由 note")
        payload = {"note": note.strip()}
        return self._append(case_id, "prescription_resumed", payload, actor_ref,
                            at=at, event_id=event_id)

    # ------------------------------------------------------------------ 信号

    def add_signal(self, case_id: str, event_type: str, payload: dict, actor_ref: str,
                   event_time: str, event_id: str | None = None,
                   ingested_at: str | None = None) -> dict:
        ordered_before = self._require_case(case_id)
        if event_type not in SIGNAL_EVENT_TYPES:
            raise ServiceError(f"不接受的信号类型: {event_type}")
        self._validate_signal(event_type, payload)
        stored = self._append(case_id, event_type, copy.deepcopy(payload), actor_ref,
                              at=event_time, event_id=event_id, ingested_at=ingested_at)

        ordered_after = self.store.load_for_replay(case_id)
        # 切点取流内最新事件时间：实时信号约等于当前时间；历史回填时不会
        # 用“现在”去裁剪旧会话而误报数据缺口。
        cutoff = ordered_after[-1]["event_time"]
        # 返回给调用方的事件使用重放版本（携带 late 标注）。
        stored = next(e for e in ordered_after if e["event_id"] == stored["event_id"])
        # 前态只折叠到“此前流内最新事件时间”，避免用新信号时间提前看到缺口。
        prev_cutoff = ordered_before[-1]["event_time"] if ordered_before else None
        before_state = self._fold(ordered_before, as_of=prev_cutoff)
        after_state = self._fold(ordered_after, as_of=cutoff)
        old_ids = {f["finding_id"] for f in before_state.findings}
        new_findings = [f for f in after_state.findings if f["finding_id"] not in old_ids]

        # 派生发现同步写入 monitor_finding 事件作为审计副本（确定性 id，
        # 重复派生不重复写入；重放时始终以原始信号重新派生为准）。
        persisted_findings = []
        for f in new_findings:
            payload = {
                "finding_id": f["finding_id"],
                "code": f["code"],
                "severity": f["severity"],
                "review_required": f["review_required"],
                "guidance": f["guidance"],
                "at": timeutil.to_iso(f["at"]),
                "source_event_id": f["source_event_id"],
                "rules_version": f["rules_version"],
                "kind": f["kind"],
                "late": f["late"],
            }
            try:
                mf = self._append(case_id, "monitor_finding", payload, "monitor-engine",
                                  at=f["at"], event_id=f"mfnd_{f['finding_id'][4:]}",
                                  ingested_at=timeutil.to_iso(timeutil.now()))
                persisted_findings.append(self._serialize_event(mf))
            except ServiceError as exc:
                if exc.code != "duplicate_event":
                    raise

        return {
            "event": self._serialize_event(stored),
            "new_findings": [self._serialize_finding(f) for f in new_findings],
            "persisted_finding_events": persisted_findings,
            "day_state": monitor.day_state(after_state),
            "paused": after_state.paused,
            "seek_emergency_care": any(
                f["severity"] == "emergency" and f["status"] == "open"
                for f in after_state.findings
            ),
        }

    @staticmethod
    def _validate_signal(event_type: str, payload: dict):
        if not isinstance(payload, dict):
            raise ServiceError("payload 必须是对象")
        if event_type == "symptom_reported":
            code = payload.get("code")
            if not isinstance(code, str) or not code:
                raise ServiceError("症状必须带 code（未知码会被安全地转人工复核）")
            if payload.get("severity") not in (None, "mild", "moderate", "severe"):
                raise ServiceError("severity 必须是 mild|moderate|severe")
        elif event_type == "device_reading":
            if not isinstance(payload.get("metric"), str):
                raise ServiceError("读数必须带 metric")
            if not isinstance(payload.get("value"), (int, float)):
                raise ServiceError("读数 value 必须是数值")
            if payload.get("quality", "ok") not in ("ok", "questionable"):
                raise ServiceError("quality 必须是 ok|questionable")
        elif event_type == "review_resolved":
            if not payload.get("finding_event_ids"):
                raise ServiceError("复核结论必须引用 finding_event_ids")
            if payload.get("resolution") not in REVIEW_RESOLUTIONS:
                raise ServiceError(f"resolution 必须是 {sorted(REVIEW_RESOLUTIONS)}")
            if not payload.get("note"):
                raise ServiceError("复核必须写明医生意见 note")

    @staticmethod
    def _serialize_finding(f: dict) -> dict:
        out = dict(f)
        out["at"] = timeutil.to_iso(f["at"])
        return out

    # ------------------------------------------------------------------ 重放

    def timeline(self, case_id: str, as_of: str | None = None) -> dict:
        ordered = self._require_case(case_id)
        opened = self._case_opened(ordered)
        tz = self._case_tz(opened)
        cutoff = timeutil.parse(as_of) if as_of else None
        events = [
            self._serialize_event(e) for e in ordered
            if cutoff is None or e["event_time"] <= cutoff
        ]

        days = {}
        for ev in ordered:
            if cutoff is not None and ev["event_time"] > cutoff:
                continue
            key = ev["event_time"].astimezone(tz).date().isoformat()
            days.setdefault(key, None)
        # 派生发现（如覆盖到期、数据缺口）可能落在没有原始事件的日子，
        # 那一天同样必须能被重放查看。
        cutoff_state = self._fold(ordered, as_of=cutoff)
        for f in cutoff_state.findings:
            days.setdefault(f["at"].astimezone(tz).date().isoformat(), None)
        # 暂停/升级闩锁期间没有新事件的日子，状态仍延续：连续填满日区间。
        if days:
            from datetime import date, timedelta as _td
            first = date.fromisoformat(min(days))
            last = date.fromisoformat(max(days))
            cur = first
            while cur <= last:
                days.setdefault(cur.isoformat(), None)
                cur += _td(days=1)

        day_views = []
        for key in sorted(days):
            local_end = datetime.fromisoformat(f"{key}T23:59:59.999999").replace(tzinfo=tz)
            day_cutoff = min(local_end, cutoff) if cutoff else local_end
            state = self._fold(ordered, as_of=day_cutoff)
            open_findings = [f for f in state.findings if f["status"] == "open"]
            day_views.append(
                {
                    "day": key,
                    "state": monitor.day_state(state),
                    "paused_by_event": state.paused,
                    "active_plan_baseline": (
                        state.active_plan["plan"].get("baseline") if state.active_plan else None
                    ),
                    "open_findings": [
                        {
                            "code": f["code"],
                            "severity": f["severity"],
                            "review_required": f["review_required"],
                            "guidance": f["guidance"],
                            "finding_id": f["finding_id"],
                        }
                        for f in open_findings
                    ],
                    "decisions": [
                        {
                            "event_id": e["event_id"],
                            "type": e["event_type"],
                            "at": timeutil.to_iso(e["event_time"]),
                            "actor_ref": e["actor_ref"],
                        }
                        for e in ordered
                        if e["event_time"] <= day_cutoff
                        and e["event_time"].astimezone(tz).date().isoformat() == key
                        and e["event_type"] in (
                            "prescription_decided", "prescription_paused",
                            "prescription_resumed", "plan_adjusted", "override_granted",
                        )
                    ],
                }
            )

        full_state = self._fold(ordered, as_of=cutoff)
        return {
            "case_id": case_id,
            "patient_ref": opened["payload"]["patient_ref"],
            "timezone": str(tz),
            "as_of": timeutil.to_iso(cutoff) if cutoff else None,
            "events": events,
            "days": day_views,
            "current_state": monitor.day_state(full_state),
            "current_plan": (
                full_state.active_plan["plan"] if full_state.active_plan else None
            ),
            "rules_versions_in_use": (
                full_state.active_plan.get("rules_versions") if full_state.active_plan else None
            ),
        }
