# 运动处方安全护栏

面向社区全科团队的超慢跑运动处方安全服务：**版本化禁忌规则先决定能否开具**，再给剂量；
患者症状与设备数据到达后按 `contracts/prescription_case.json` 的事件语义识别偏离、暂停条件
与人工复核需求；任意一天的获准、暂停或升级决定都可完整重放。

## 安全原则（不可协商）

1. **规则先于处方**：存在绝对禁忌时系统强制阻断，任何医生覆盖都无效，不生成处方版本。
2. **缺失不等于安全（fail-closed）**：前置事件缺失、乱序、迟到、缺口、未知类型或载荷非法，
   当日一律不予获准（`WITHHOLD`/`PAUSE`），并生成人工复核需求。
3. **紧急症状只给就医提示**：胸痛、晕厥、静息呼吸困难等只输出"立即急诊/拨打 120"提示，
   系统不自动诊断、不解释病因。
4. **覆盖可追责**：医生覆盖相对禁忌必须同时写明临床理由、权限级别（`ATTENDING`/`SPECIALIST`）
   与有效期（≤30 天），到期自动失效；覆盖只对被点名的规则生效，且仍按高风险人群降档。
5. **全程留痕可重放**：所有记录写入只追加的哈希链账本；处方每次调整保留 before/after 版本；
   给定规则包版本 + 当日处方版本 + 当日生效覆盖 + 当日事件，重放结果逐字节一致。

## 决策码

| 决策 | 含义 |
|---|---|
| `APPROVE` | 前置闸门通过且安全完成一次会话 |
| `WITHHOLD` | 数据不足/未过安全前置（缺数据、乱序、闸门失败等），不予获准 |
| `PAUSE` | 触发暂停条件，立即停止当日活动（含不安全开跑、连续心率越限等） |
| `ESCALATE` | 偏离累计达阈值等，需要人工复核 |
| `EMERGENCY_ADVICE` | 紧急症状，仅给出明确就医提示，不做诊断 |
| `NOT_PRESCRIBABLE` | 存在绝对禁忌，不能开具 |
| `NO_SESSION` | 当日无活动（仅通过闸门但未开跑也算），不代表安全获准 |

## 模块结构

- `prescription/rules.py` — 不可变规则包 `slow-jogging-safety@1.0.0`（绝对/相对/自适应三级禁忌）、
  录入完整性与指标陈旧检查、剂量计算（配速、HRR 心率区间、时长、频次、热身、递增率、监测要求）。
- `prescription/events.py` — 事件信封与流校验：`MALFORMED / MISSING_FIELD / UNKNOWN_TYPE /
  OUT_OF_ORDER / GAP / LATE / DUPLICATE`，乱序事件不参与自动决策。
- `prescription/engine.py` — 按自然日重放：症状分级、血压/心率/血糖硬闸门、连续越限暂停、
  偏离累计升级、日终完整性检查；输出逐步 `trace`。
- `prescription/store.py` — JSONL 追加账本，每条记录含前一条 SHA-256，篡改即不可加载。
- `prescription/flows.py` — 开具、调整、暂停、覆盖、事件接入、人工复核留痕与按日重放；
  复评出现新禁忌时系统自动暂停生效处方。
- `service.py` — 标准库 HTTP 接口（无第三方运行时依赖）。

## HTTP 接口

```
GET  /health                                 服务身份
GET  /v1/rule-packs                          规则包清单（规则码、分级、阈值、剂量默认值）
POST /v1/patients/<ref>/assessments          录入评估 {assessment, clinician_id, today}
POST /v1/patients/<ref>/prescriptions        开具（先过禁忌规则）
POST /v1/patients/<ref>/adjustments          调整 {changes, reason, today}（理由必填）
POST /v1/patients/<ref>/suspensions          手动暂停 {reason, today}
POST /v1/patients/<ref>/overrides            覆盖 {clinician_id, authority, rule_codes,
                                                reason, valid_until, today}
POST /v1/events                              上报症状/设备事件（原始内容入账）
GET  /v1/patients/<ref>/days/<YYYY-MM-DD>    该日完整重放（决定 + 逐步 trace + 质量标记）
GET  /v1/patients/<ref>/history              处方版本链与覆盖记录
POST /v1/reviews                             记录人工复核结论
GET  /v1/ledger/verify                       哈希链完整性校验
```

绝对禁忌开具返回 `403 NOT_PRESCRIBABLE`；资料不全/指标陈旧返回 `422`；覆盖绝对禁忌返回
`403 ABSOLUTE_NOT_OVERRIDABLE`；调整超出安全包络或违反递增规则返回 `422`。

### 递增规则

- 每周时长增幅 ≤10%（自适应人群 ≤5%），频次每次至多 +1；
- 顺序固定：**先时长，再频次，最后提速**；一次调整只能加一个维度；
- 近 7 天出现 `PAUSE / EMERGENCY_ADVICE / ESCALATE / WITHHOLD` 的一周**不得加量**（减量允许）；
- 更慢、更短、更少（低于推荐区间下界）不增加风险，允许医生保守下调，但不得超过安全上限。

## 运行

```bash
python3 service.py --check                      # 自检
python3 service.py --port 8000 --ledger data/ledger.jsonl
PRESCRIPTION_LEDGER=/path/to.jsonl python3 service.py
```

## 测试

```bash
python3 -m unittest discover -s tests -v        # 82 个用例（环境无 pytest 时）
python3 -m pytest tests/                        # 安装 requirements.txt 后等价
```

用例覆盖：三级禁忌与剂量、录入缺失/陈旧、覆盖权限与有效期、自动暂停、前后版本、
递增顺序/增幅/不安全周阻断、紧急就医提示、缺失/乱序/非法载荷故障关闭、跨日重放确定性、
账本篡改检测与 HTTP 端到端。

## 契约

`contracts/prescription_case.json` 保存公开的领域样例与事件语义（事件类型、必填字段、
症状分级、数据质量标记、版本与覆盖规则）。样例仅含伪匿名数据，不含真实个人资料、
业务凭据或生产连接信息。
