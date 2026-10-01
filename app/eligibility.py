"""禁忌裁决。

裁决顺序（失败安全）：
1. 绝对禁忌命中 -> contraindicated
2. 复核规则命中（信息不足）-> needs_review，绝不推断为安全
3. 相对禁忌命中 -> eligible，但附带降阶修饰
医生覆盖仅在有效期内、且携带理由与权限时生效；被覆盖的绝对禁忌
仍会在结果中留痕，并强制要求医生监督起步。
"""

from .conditions import evaluate


def _matching(rules: list, ctx: dict) -> list:
    return [r["code"] for r in rules if evaluate(r["when"], ctx)]


def evaluate_eligibility(assessment: dict, ci_rules: dict, active_overrides: dict | None = None) -> dict:
    """active_overrides: {rule_code: override_payload}，只包含当前有效的覆盖。"""
    active_overrides = active_overrides or {}
    ctx = {"assessment": assessment}

    absolute_hits = _matching(ci_rules["absolute"], ctx)
    review_hits = _matching(ci_rules["review"], ctx)
    relative_hits = _matching(ci_rules["relative"], ctx)

    overridden = sorted(c for c in list(absolute_hits) + list(review_hits) if c in active_overrides)

    blocked_absolute = [c for c in absolute_hits if c not in active_overrides]
    pending_review = [c for c in review_hits if c not in active_overrides]

    if blocked_absolute:
        outcome = "contraindicated"
    elif pending_review:
        outcome = "needs_review"
    else:
        outcome = "eligible"

    return {
        "outcome": outcome,
        "absolute_hits": sorted(absolute_hits),
        "review_hits": sorted(review_hits),
        "relative_hits": sorted(relative_hits),
        "overridden_codes": overridden,
        "overrides": [
            {
                "rule_code": code,
                "reason": active_overrides[code]["reason"],
                "authority": active_overrides[code]["authority"],
                "valid_until": active_overrides[code]["valid_until"],
            }
            for code in overridden
        ],
        "dose_modifiers": [
            r.get("dose_modifier")
            for code in relative_hits
            for r in ci_rules["relative"]
            if r["code"] == code and r.get("dose_modifier")
        ],
        "requires_physician_supervision": bool(
            [c for c in overridden if c in absolute_hits]
        ),
    }
