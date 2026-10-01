# 运动处方安全护栏

帮助社区临床团队开具超慢跑等低冲击运动处方，并在患者运动过程中持续识别禁忌、偏离与急症。**速度慢不等于人人安全**：严重关节损伤、急性运动损伤、心肺疾病、血压血糖不稳的居民必须先经过版本化禁忌规则裁决，信息缺失一律失败安全（needs_review / 暂停），绝不默认安全。

## 安全模型

1. **先裁决，后开方**。`contraindications` 规则先给出 `eligible | contraindicated | needs_review`；只有 eligible 才生成配速、心率区间（HRR 40–59%）、RPE 11–13、时长、频次、热身/放松与递增安排。使用 beta 阻滞剂等抑制心率药物时只给 RPE 与说话测试区间。
2. **规则版本化并钉选**。规则位于 `rules/<ruleset>/<version>.json`（禁忌、剂量、监测三套）。每次裁决、调整、发现都记录所用版本；历史重放使用事件中钉选的版本，不用新规则改写历史。
3. **医生覆盖必须留痕**。覆盖单条规则必须提供 `reason`（理由）、`authority`（权限角色白名单）、`valid_until`（有效期）。到期后失败安全回到规则默认禁忌，自动产生 `OVERRIDE_EXPIRED` 暂停发现；基于覆盖开具的处方首次运动须医务人员监督。
4. **缺失与乱序不安全**。许可、年龄、血压、糖尿病患者血糖等关键信息缺失 → 需人工复核；运动中读数中断超过阈值（默认 300 秒）或会话未闭合 → `DATA_GAP` 暂停；未登记症状/测量码 → `UNKNOWN_SIGNAL` 转人工；`questionable` 读数不算区间偏离但计入在场；乱序/晚到数据标注 `late`，不回改历史，只在重放视图中体现。
5. **急症只提示就医，不自动诊断**。胸痛、晕厥、静息呼吸困难等输出明确的急救/就医指导与 `escalated` 状态，不给出任何诊断结论。
6. **暂停闩锁**。暂停/升级一旦发生，即使触发发现被人工复核关闭，也必须由医生显式 `resume`（写明理由）或重新裁决合格才解除。
7. **调整留下前后版本**。`plan_adjusted` 同时保存 before/after 完整计划、变更字段与理由，并施加结构护栏（极端配速/时长须走规则修订而非单次处方突破）。
8. **任意一天可完整重放**。所有结论来自只追加的 JSONL 事件账本（`data/ledger/<case>.jsonl`），按 `(event_time, event_id)` 确定性折叠；当前时间只用于写入新事件，不参与历史重放。

## 事件语义

外部契约与词汇表见 [`contracts/prescription_case.json`](contracts/prescription_case.json)：事件信封、全部事件类型（开案、裁决、覆盖、调整、暂停、恢复、症状、设备读数、会话开始/结束、监测发现、复核结论）、发现码/症状码词汇表、缺失与乱序语义、逐日状态（`allowed | suspended | escalated | not_evaluated`）。契约样例只含伪匿名数据。

## HTTP 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/health` | 服务身份 |
| GET | `/v1/rules`、`/v1/rules/<ruleset>/<version>` | 规则目录与内容 |
| POST | `/v1/cases` | 开案：诊断、近期指标、用药、活动基础、许可意见 |
| POST | `/v1/cases/<id>/decisions` | 按当前规则版本裁决并生成处方 |
| POST | `/v1/cases/<id>/overrides` | 医生覆盖（reason + authority + valid_until） |
| POST | `/v1/cases/<id>/adjustments` | 计划调整（保存前后版本） |
| POST | `/v1/cases/<id>/reassessments` | 复评（覆盖到期/情况变化可触发暂停） |
| POST | `/v1/cases/<id>/resume` | 医生恢复（必须带理由） |
| POST | `/v1/cases/<id>/events` | 症状/设备/会话/复核信号接入 |
| GET | `/v1/cases/<id>/timeline?as_of=ISO8601` | 完整事件流 + 逐日可重放结论 |

所有时间必须是带时区的 ISO-8601；`event_id` 病例内唯一、投递幂等；设备事件可用 `ingested_at` 表达真实接收时刻以识别乱序。

## 运行

```bash
python3 service.py --check                 # 身份与规则包自检
python3 service.py --port 8000             # 启动（账本目录可用 --ledger-dir 或 PSE_LEDGER_DIR 覆盖）
python3 -m unittest discover -s tests -v   # 全部测试（53 项）
```

示例：

```bash
curl -s -X POST localhost:8000/v1/cases -H 'Content-Type: application/json' -d '{
  "patient_ref": "PSEUDO-001", "actor_ref": "dr.li",
  "at": "2026-10-01T08:00:00+08:00",
  "assessment": {
    "age": 55, "activity_baseline": "sedentary", "clinical_permission": "granted",
    "recent_metrics": {"resting_sbp_mmhg": 128, "resting_dbp_mmhg": 80,
                       "resting_hr_bpm": 70, "fasting_glucose_mmol_l": 6.0},
    "medications": [], "diagnoses": []}}'
```

## 代码结构

```
contracts/prescription_case.json   对外事件契约与词汇表
rules/<ruleset>/<version>.json     版本化规则包（contraindications/dosing/monitoring）
app/conditions.py                  失败安全条件求值（缺失路径不参与肯定判断）
app/registry.py                    规则版本加载与钉选
app/eligibility.py                 禁忌裁决（绝对/复核/相对 + 覆盖）
app/dosing.py                      配速/心率/时长/频次/热身/递增
app/monitor.py                     事件流确定性折叠：偏离、缺口、急症、到期、逐日状态
app/ledger.py                      只追加 JSONL 账本、幂等、乱序标注
app/facade.py                      用例门面与逐日重放
service.py                         HTTP 入口
```
