"""运动处方安全服务领域包。

模块划分：
- rules:  不可变版本化禁忌规则包、筛查与剂量计算
- events: 事件校验（缺失/乱序/迟到/未知类型）
- engine: 日决策引擎（偏离、暂停、升级、紧急就医提示、重放）
- store:  JSONL 追加账本（处方版本、覆盖、事件）
- flows:  开具、覆盖、事件接入、按日重放等应用流程
"""

SERVICE_ID = "exercise-prescription-safety"
