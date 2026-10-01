"""监测引擎：把症状/设备/会话事件折叠为确定性的发现流与日状态。

重要语义：
- find 结果是从原始信号事件 *派生* 的；重放不依赖已存储的 monitor_finding
  事件，因此晚到数据会自然反映在重放视图中，而实时告警仍以账本中
  已存储的发现为准（不回改历史）。
- 派生发现使用确定性 id，使人工复核结论（review_resolved）可以跨重放引用。
- 缺失/缺口失败安全：会话中读数中断超过阈值即 DATA_GAP 并暂停；
  未闭合会话在重放切点同样按缺口处理。
"""

from dataclasses import dataclass, field
from datetime import timedelta
from typing import Optional

from . import timeutil

SEVERITY_RANK = ["info", "caution", "suspend", "emergency"]


def _rank(sev: str) -> int:
    return SEVERITY_RANK.index(sev)


def _escalate(sev: str, reported: Optional[str], policy: dict) -> str:
    """按患者自报严重程度上调：moderate 升一级；severe 至少 suspend。"""
    if reported == "moderate":
        return SEVERITY_RANK[min(len(SEVERITY_RANK) - 1, _rank(sev) + 1)]
    if reported == "severe":
        return sev if _rank(sev) >= _rank("suspend") else "suspend"
    return sev


@dataclass
class _Session:
    event_id: str
    started_at: object
    last_signal_at: object = None
    gap_count: int = 0


@dataclass
class FoldState:
    rules_version: Optional[str] = None
    active_plan: Optional[dict] = None
    paused: bool = False
    hold: bool = False  # 暂停/升级一旦发生即闩锁，只有显式恢复或新合格裁决解除
    pause_payload: Optional[dict] = None
    open_session: Optional[_Session] = None
    hr_out_streak: int = 0
    findings: list = field(default_factory=list)  # 按发生时间排序
    overrides: dict = field(default_factory=dict)  # event_id -> override payload
    used_override_codes: set = field(default_factory=set)
    last_clock: Optional[object] = None

    def add_finding(self, f: dict):
        self.findings.append(f)
        if f["severity"] in ("suspend", "emergency"):
            self.hold = True

    def finding_index(self) -> dict:
        return {f["finding_id"]: f for f in self.findings}


def _finding(fid, code, severity, guidance, at, source_event_id, rules_version,
             review_required=False, late=False, kind="signal"):
    return {
        "finding_id": fid,
        "code": code,
        "severity": severity,
        "review_required": review_required,
        "guidance": guidance,
        "at": at,
        "source_event_id": source_event_id,
        "rules_version": rules_version,
        "status": "open",
        "resolved_by": None,
        "resolution": None,
        "late": late,
        "kind": kind,  # signal | gap | expiry | late
    }


def _versioned_rules(registry, state: FoldState, ruleset: str, default_rules: dict) -> dict:
    version = (
        state.active_plan.get("rules_versions", {}).get(ruleset)
        if state.active_plan
        else None
    )
    return registry.get(ruleset, version) if version else default_rules


def _resolve_matching(state: FoldState, finding_ids, resolution: str, by_event_id: str):
    index = state.finding_index()
    for fid in finding_ids:
        f = index.get(fid)
        if f and f["status"] == "open":
            # 人工复核只关闭问题；真正解除暂停必须由医生显式恢复
            # （prescription_resumed），复核本身不自动放行。
            f["status"] = "reviewed"
            f["resolved_by"] = by_event_id
            f["resolution"] = resolution


def _expire_overrides(state: FoldState, until, registry, default_monitor_rules):
    """检查在 (last_clock, until] 之间到期、且仍被当前决策依赖的覆盖。"""
    for oid, ov in list(state.overrides.items()):
        if state.last_clock is None:
            continue
        expires_at = timeutil.parse(ov["valid_until"])
        if not (state.last_clock < expires_at <= until):
            continue
        if ov["rule_code"] not in state.used_override_codes:
            continue
        if state.active_plan is None and not state.paused:
            continue
        rules = _versioned_rules(registry, state, "monitoring", default_monitor_rules)
        fid = f"fnd:{oid}:expired"
        if any(f["finding_id"] == fid for f in state.findings):
            continue
        state.add_finding(
            _finding(
                fid,
                "OVERRIDE_EXPIRED",
                "suspend",
                "医生覆盖已到期，规则默认禁忌重新生效；暂停运动并由医生重新评估后开具。",
                expires_at,
                oid,
                rules["rules_version"],
                review_required=True,
                kind="expiry",
            )
        )


def _gap_check(state: FoldState, at, rules, force=False):
    """对开放会话做数据缺口检查；force=True 用于会话结束/重放切点。"""
    sess = state.open_session
    if sess is None:
        return []
    # 从未收到读数时，以会话开始时间为基准：长时间静默同样是数据缺口。
    base = sess.last_signal_at or sess.started_at
    gap = timedelta(seconds=rules["data_gap"]["default_max_gap_seconds"])
    out = []
    while at - base > gap:
        sess.gap_count += 1
        crossed_at = base + gap
        fid = f"fnd:{sess.event_id}:gap{sess.gap_count}"
        f = _finding(
            fid,
            rules["data_gap"]["finding_code"],
            rules["data_gap"]["severity"],
            rules["data_gap"]["guidance"],
            crossed_at,
            sess.event_id,
            rules["rules_version"],
            review_required=rules["data_gap"].get("review_required", True),
            kind="gap",
        )
        out.append(f)
        state.add_finding(f)
        # 缺口发现后，把基准前移，持续中断按周期间隔重复升级。
        sess.last_signal_at = crossed_at
        base = crossed_at
        if not force:
            break
    return out


def _apply_symptom(state, ev, rules):
    payload = ev["payload"] or {}
    policy = rules["symptom_policy"]
    code = payload.get("code")
    reported = payload.get("severity")
    if code not in policy or code == "unknown_code":
        spec = policy["unknown_code"]
        base_sev = spec["severity"]
    else:
        spec = policy[code]
        base_sev = spec["severity"]
    severity = _escalate(base_sev, reported, rules)
    finding_code = spec.get("finding_code")
    if not finding_code:
        if _rank(severity) < _rank("caution"):
            return  # info 级且无发现码：只留症状事件。
        # 自报严重程度把原本 info 的症状推高时，转人工关注。
        finding_code = "MANUAL_REVIEW_REQUIRED"
        guidance = "症状自报程度偏高，请降低强度并联系医务人员评估。"
    else:
        guidance = spec["guidance"]
    fid = f"fnd:{ev['event_id']}:1"
    state.add_finding(
        _finding(
            fid,
            finding_code,
            severity,
            guidance,
            ev["event_time"],
            ev["event_id"],
            rules["rules_version"],
            review_required=spec.get("review_required", False) or severity in ("suspend", "emergency"),
        )
    )


def _in_range(value, rng):
    return rng[0] <= value <= rng[1]


def _apply_reading(state, ev, rules):
    payload = ev["payload"] or {}
    metric = payload.get("metric")
    value = payload.get("value")
    quality = payload.get("quality", "ok")
    plan = state.active_plan["plan"] if state.active_plan else None
    at = ev["event_time"]

    # 任何读数都代表设备仍在上报，用于数据缺口判定（questionable 也算在场）。
    if state.open_session is not None:
        _gap_check(state, at, rules)
        state.open_session.last_signal_at = at

    if quality == "questionable":
        return  # 可疑数值不参与区间判定。
    if not isinstance(value, (int, float)):
        return

    rp = rules["reading_policy"]
    if metric == "heart_rate_bpm":
        zone = (plan or {}).get("hr_zone")
        if not zone:
            # beta 阻滞剂或缺少区间：不做判定，也不累积连击。
            state.hr_out_streak = 0
            return
        lo, hi = zone["bpm"]
        if _in_range(value, (lo, hi)):
            state.hr_out_streak = 0
            return
        state.hr_out_streak += 1
        spec = rp["heart_rate_bpm"]
        if state.hr_out_streak >= spec["consecutive_excursions_for_suspend"]:
            severity = "suspend"
        else:
            severity = spec["first_excursion"]
        code = spec["above_finding_code"] if value > hi else spec["below_finding_code"]
        guidance = spec["above_guidance"] if value > hi else spec["below_guidance"]
        state.add_finding(
            _finding(
                f"fnd:{ev['event_id']}:hr",
                code,
                severity,
                guidance,
                at,
                ev["event_id"],
                rules["rules_version"],
                review_required=severity == "suspend",
            )
        )
    elif metric == "pace_kmh":
        rng = (plan or {}).get("pace_kmh")
        if rng and not _in_range(value, rng):
            spec = rp["pace_kmh"]
            state.add_finding(
                _finding(
                    f"fnd:{ev['event_id']}:2",
                    spec["finding_code"],
                    spec["severity"],
                    spec["guidance"],
                    at,
                    ev["event_id"],
                    rules["rules_version"],
                    review_required=False,
                )
            )
    else:
        spec = rp["unknown_metric"]
        state.add_finding(
            _finding(
                f"fnd:{ev['event_id']}:1",
                spec["finding_code"],
                spec["severity"],
                spec["guidance"],
                at,
                ev["event_id"],
                rules["rules_version"],
                review_required=spec.get("review_required", True),
            )
        )


def _apply_session(state, ev, rules):
    at = ev["event_time"]
    if ev["event_type"] == "session_started":
        if state.open_session is not None:
            # 未闭合又开始新会话：旧会话按缺口失败安全收尾。
            _close_session(state, state.open_session, at, rules, unclosed=True)
        sess = _Session(event_id=ev["event_id"], started_at=at)
        state.open_session = sess
    elif ev["event_type"] == "session_ended":
        target = state.open_session
        if target is None:
            # 已暂停后迟到的结束事件：暂停原因已经充分，不再叠加缺口发现；
            # 其他情况下“有结束无开始”按数据缺口失败安全处理。
            if state.hold or state.paused:
                return
            spec = rules["data_gap"]
            state.add_finding(
                _finding(
                    f"fnd:{ev['event_id']}:orphan",
                    "DATA_GAP",
                    spec["severity"],
                    "缺少会话开始记录，无法确认运动过程安全，需人工复核。",
                    at,
                    ev["event_id"],
                    rules["rules_version"],
                    review_required=True,
                    kind="gap",
                )
            )
            return
        _close_session(state, target, at, rules, end_event=ev)


def _close_session(state, sess, at, rules, unclosed=False, end_event=None):
    # 结束前做最后一次缺口检查（force 覆盖整个尾部间隔）。
    _gap_check(state, at, rules, force=True)
    state.open_session = None

    duration = None
    if end_event is not None:
        duration = (end_event.get("payload") or {}).get("duration_minutes")
    if duration is None:
        duration = (at - sess.started_at).total_seconds() / 60.0

    if unclosed:
        spec = rules["data_gap"]
        state.add_finding(
            _finding(
                f"fnd:{sess.event_id}:unclosed",
                "DATA_GAP",
                spec["severity"],
                "上一次运动缺少结束记录，按数据缺失失败安全处理，需人工复核。",
                at,
                sess.event_id,
                rules["rules_version"],
                review_required=True,
                kind="gap",
            )
        )

    plan = state.active_plan["plan"] if state.active_plan else None
    # 只有收到明确的结束事件才做时长判定；未闭合会话只按数据缺口处理。
    if end_event is not None and plan is not None and isinstance(duration, (int, float)):
        spec = rules["reading_policy"]["session_minutes"]
        tol = spec.get("tolerance_pct", 0) / 100.0
        lo, hi = plan["minutes"]
        if duration < lo * (1 - tol) or duration > hi * (1 + tol):
            state.add_finding(
                _finding(
                    f"fnd:{end_event['event_id']}:duration",
                    spec["finding_code"],
                    spec["severity"],
                    spec["guidance"],
                    at,
                    anchor,
                    rules["rules_version"],
                    review_required=False,
                )
            )


def fold(events, registry, default_monitor_rules, as_of=None, include_late=True):
    """对已按 (event_time, event_id) 排序的事件做确定性折叠。

    每个事件可带注解键 ``late``（相对接收顺序为晚到）。
    返回 FoldState；findings 按发生时间排序。
    """
    state = FoldState(rules_version=default_monitor_rules["rules_version"])
    late_rules = default_monitor_rules  # 乱序提示使用默认版本即可（info 级）

    for ev in events:
        if as_of is not None and ev["event_time"] > as_of:
            break
        if state.last_clock is not None and ev["event_time"] > state.last_clock:
            _expire_overrides(state, ev["event_time"], registry, default_monitor_rules)
        state.last_clock = ev["event_time"]
        etype = ev["event_type"]

        if etype == "prescription_decided":
            payload = ev["payload"] or {}
            # 新决策使此前的覆盖到期暂停失效（已用新评估替代）。
            for f in state.findings:
                if f["kind"] == "expiry" and f["status"] == "open":
                    f["status"] = "reviewed"
                    f["resolution"] = "superseded_by_decision"
                    f["resolved_by"] = ev["event_id"]
            snapshot = payload.get("plan_snapshot")
            if payload.get("outcome") == "eligible" and snapshot and snapshot.get("plan"):
                state.active_plan = dict(snapshot, decided_at=ev["event_time"])
                state.used_override_codes = set(snapshot.get("overridden_codes") or [])
                # 重新裁决合格即解除暂停（暂停事件本身仍保留在流中）。
                state.paused = False
                state.hold = False
                state.pause_payload = None
            else:
                state.active_plan = None
                state.used_override_codes = set()

        elif etype == "override_granted":
            p = ev["payload"]
            state.overrides[ev["event_id"]] = p

        elif etype == "plan_adjusted":
            p = ev["payload"] or {}
            after = p.get("after")
            if after and after.get("plan"):
                state.active_plan = dict(after, decided_at=ev["event_time"])
                state.used_override_codes = set(after.get("overridden_codes") or state.used_override_codes)
            state.hr_out_streak = 0

        elif etype == "prescription_paused":
            state.paused = True
            state.hold = True
            state.pause_payload = ev.get("payload")
            state.hr_out_streak = 0
            if state.open_session is not None:
                # 暂停原因本身已足够，未闭合会话静默收尾（不做时长判定，
                # 也不叠加“缺少结束记录”）；之后迟到的 ended 会被忽略。
                state.open_session = None

        elif etype == "prescription_resumed":
            state.paused = False
            state.hold = False
            state.pause_payload = None
            # 医生显式恢复：关闭全部仍开放的发现与到期暂停。
            for f in state.findings:
                if f["status"] == "open":
                    f["status"] = "resolved"
                    f["resolution"] = "resumed_by_physician"
                    f["resolved_by"] = ev["event_id"]

        elif etype == "review_resolved":
            p = ev["payload"] or {}
            _resolve_matching(state, p.get("finding_event_ids") or [],
                              p.get("resolution", "keep_suspended"), ev["event_id"])

        elif etype == "symptom_reported":
            _apply_symptom(state, ev, _versioned_rules(registry, state, "monitoring", default_monitor_rules))

        elif etype == "device_reading":
            _apply_reading(state, ev, _versioned_rules(registry, state, "monitoring", default_monitor_rules))

        elif etype in ("session_started", "session_ended"):
            _apply_session(state, ev, _versioned_rules(registry, state, "monitoring", default_monitor_rules))

        if include_late and ev.get("late") and etype in ("device_reading", "symptom_reported", "session_started", "session_ended"):
            spec = late_rules["late_data"]
            state.add_finding(
                _finding(
                    f"fnd:{ev['event_id']}:late",
                    spec["finding_code"],
                    spec["severity"],
                    spec["guidance"],
                    ev["event_time"],
                    ev["event_id"],
                    late_rules["rules_version"],
                    review_required=False,
                    late=True,
                    kind="late",
                )
            )

    # 重放切点：检查尚未触发的覆盖到期。
    cutoff = as_of or state.last_clock
    if cutoff is not None:
        _expire_overrides(state, cutoff, registry, default_monitor_rules)
        if state.open_session is not None:
            _gap_check(state, cutoff,
                       _versioned_rules(registry, state, "monitoring", default_monitor_rules))

    state.findings.sort(key=lambda f: (f["at"], f["finding_id"]))
    return state


def day_state(state: FoldState) -> str:
    """按 day_state_rules 将折叠快照映射为当日状态。

    暂停/升级一旦发生即闩锁（hold），即使原始发现被人工复核关闭，
    没有医生显式 resume 或新的合格裁决也不得回到 allowed。
    """
    if state.active_plan is None:
        # 从未开方：not_evaluated；曾被暂停/升级但没有新生效处方：仍 suspended。
        return "suspended" if (state.paused or state.hold) else "not_evaluated"
    open_findings = [f for f in state.findings if f["status"] == "open"]
    if any(f["severity"] == "emergency" for f in open_findings):
        return "escalated"
    if state.paused or state.hold or any(f["severity"] == "suspend" for f in open_findings):
        return "suspended"
    if any(f["severity"] == "caution" and f.get("review_required") for f in open_findings):
        return "escalated"
    return "allowed"
