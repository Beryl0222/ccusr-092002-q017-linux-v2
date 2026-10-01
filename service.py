"""运动处方安全护栏服务入口（标准库 HTTP，无第三方运行时依赖）。

路由：
  GET  /health                                 服务身份
  GET  /v1/rule-packs                          规则包与禁忌规则清单
  POST /v1/patients/<ref>/assessments          录入评估（自动禁忌筛查/必要时联动暂停）
  POST /v1/patients/<ref>/prescriptions        开具（规则先决定能否开具）
  POST /v1/patients/<ref>/adjustments          调整（前后版本留痕、递增约束）
  POST /v1/patients/<ref>/suspensions          暂停处方
  POST /v1/patients/<ref>/overrides            医生覆盖（理由/权限/有效期）
  POST /v1/events                              上报症状/设备事件（仅入账）
  GET  /v1/patients/<ref>/days/<YYYY-MM-DD>    任意一天的完整重放
  GET  /v1/patients/<ref>/history              处方版本与覆盖历史
  POST /v1/reviews                             记录人工复核结论
  GET  /v1/ledger/verify                       哈希链完整性校验
"""

import argparse
import json
import os
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from prescription import SERVICE_ID
from prescription.flows import SafetyService
from prescription.rules import DEFAULT_PACK_ID, PACKS
from prescription.store import AppendOnlyStore

SERVICE_NAME = "运动处方安全护栏"

DEFAULT_LEDGER = os.environ.get("PRESCRIPTION_LEDGER", "data/ledger.jsonl")


def health_payload():
    """返回稳定的服务身份信息。"""
    return {"status": "ok", "service": SERVICE_ID, "name": SERVICE_NAME}


def build_service(path: str = DEFAULT_LEDGER, pack_id: str = DEFAULT_PACK_ID) -> SafetyService:
    return SafetyService(AppendOnlyStore(path), pack_id=pack_id)


def rule_pack_payload(pack_id: str = DEFAULT_PACK_ID) -> dict:
    pack = PACKS[pack_id]
    return {
        "pack_id": pack.pack_id,
        "version": pack.version,
        "full_id": pack_id,
        "issued_on": pack.issued_on,
        "metric_max_age_days": pack.metric_max_age_days,
        "thresholds": pack.thresholds,
        "dose_defaults": pack.dose_defaults,
        "rules": [
            {"code": r.code, "level": r.level, "title": r.title,
             "required_specialty": r.required_specialty}
            for r in pack.rules
        ],
        "notes": pack.notes,
    }


class Handler(BaseHTTPRequestHandler):
    service: SafetyService = None  # 由 main 注入

    # ------------------------------------------------------------------
    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        if path == "/health":
            self._json(200, health_payload())
            return
        if path == "/v1/rule-packs":
            self._json(200, {"packs": [rule_pack_payload(pid) for pid in PACKS]})
            return
        if path == "/v1/ledger/verify":
            self._json(200, self.service.store.verify())
            return
        if path.startswith("/v1/patients/"):
            rest = path[len("/v1/patients/"):].split("/")
            if len(rest) == 2 and rest[1] == "history":
                self._json(200, self.service.history(rest[0]))
                return
            if len(rest) == 3 and rest[1] == "days":
                try:
                    self._json(200, self.service.replay_day(rest[0], rest[2]))
                except ValueError:
                    self._json(400, {"ok": False, "code": "BAD_DAY",
                                     "message": "日期必须是 YYYY-MM-DD"})
                return
        self.send_error(404)

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        body = self._read_body()
        if body is None:
            return

        if path == "/v1/events":
            result = self.service.ingest_event(body)
            # REJECTED_EVENT 已入账留痕，用 422 告诉调用方该载荷不可信
            self._json(200 if result.ok else 422, result.to_dict())
            return
        if path == "/v1/reviews":
            result = self.service.record_manual_review(
                body.get("patient_ref", ""), body.get("day", ""),
                body.get("clinician_id", ""), body.get("action", ""),
                body.get("note", ""))
            self._json(200 if result.ok else 400, result.to_dict())
            return
        if path.startswith("/v1/patients/"):
            rest = path[len("/v1/patients/"):].split("/")
            if len(rest) == 2 and rest[1] == "assessments":
                result = self.service.record_assessment(
                    rest[0], body.get("assessment", body), body.get("clinician_id", ""),
                    _day(body))
                self._json(200, result.to_dict())
                return
            if len(rest) == 2 and rest[1] == "prescriptions":
                result = self.service.prescribe(rest[0], body.get("clinician_id", ""), _day(body))
                status = 200 if result.ok else _block_status(result.code)
                self._json(status, result.to_dict())
                return
            if len(rest) == 2 and rest[1] == "adjustments":
                result = self.service.adjust(
                    rest[0], body.get("clinician_id", ""),
                    body.get("changes", {}), body.get("reason", ""), _day(body))
                self._json(200 if result.ok else 422, result.to_dict())
                return
            if len(rest) == 2 and rest[1] == "suspensions":
                result = self.service.suspend(
                    rest[0], body.get("clinician_id", ""), body.get("reason", ""), _day(body))
                self._json(200 if result.ok else 400, result.to_dict())
                return
            if len(rest) == 2 and rest[1] == "overrides":
                result = self.service.grant_override(
                    rest[0], body.get("clinician_id", ""), body.get("authority", ""),
                    body.get("rule_codes", []), body.get("reason", ""),
                    body.get("valid_until", ""), _day(body))
                self._json(200 if result.ok else 403, result.to_dict())
                return
        self.send_error(404)

    # ------------------------------------------------------------------
    def _read_body(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._json(400, {"ok": False, "code": "BAD_REQUEST", "message": "Content-Length 非法"})
            return None
        raw = self.rfile.read(length) if length else b"{}"
        try:
            data = json.loads(raw.decode("utf-8"))
            if not isinstance(data, dict):
                raise ValueError
            return data
        except (ValueError, UnicodeDecodeError):
            self._json(400, {"ok": False, "code": "BAD_JSON", "message": "请求体必须是 JSON 对象"})
            return None

    def _json(self, status: int, payload: dict):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        return


def _day(body: dict) -> date:
    value = body.get("today")
    return date.fromisoformat(value) if isinstance(value, str) else date.today()


def _block_status(code: str) -> int:
    # 绝对禁忌是正常的业务结论，用 403 表达"系统拒绝"；资料不足用 422
    return 403 if code == "NOT_PRESCRIBABLE" else 422


def main():
    parser = argparse.ArgumentParser(description=SERVICE_NAME)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--ledger", default=DEFAULT_LEDGER)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if args.check:
        assert health_payload()["service"] == SERVICE_ID
        rule_pack_payload()  # 规则包可加载
        print("基础检查通过")
        return
    Handler.service = build_service(args.ledger)
    print(f"{SERVICE_NAME} 启动：账本 {args.ledger}，规则包 {DEFAULT_PACK_ID}")
    ThreadingHTTPServer(("0.0.0.0", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
