"""运动处方安全护栏服务入口。

HTTP 接口：
  GET  /health
  GET  /v1/rules                              规则目录
  GET  /v1/rules/<ruleset>/<version>          查看某版本规则包
  POST /v1/cases                              开案
  POST /v1/cases/<id>/decisions               按当前版本规则裁决能否开具
  POST /v1/cases/<id>/overrides               医生覆盖（理由+权限+有效期）
  POST /v1/cases/<id>/adjustments             计划调整（保存前后版本）
  POST /v1/cases/<id>/reassessments           复评（可触发暂停）
  POST /v1/cases/<id>/resume                  医生恢复
  POST /v1/cases/<id>/events                  症状/设备/会话/复核信号接入
  GET  /v1/cases/<id>/timeline?as_of=ISO      完整事件流与逐日可重放结论

所有结论只追加落盘于 LEDGER 目录的 JSONL，可离线重放。
"""

import argparse
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from app.facade import PrescriptionService, ServiceError
from app.ledger import EventStore
from app.registry import RuleRegistry

SERVICE_ID = "exercise-prescription-safety"
SERVICE_NAME = "运动处方安全护栏"

_SERVICE = None


def get_service():
    global _SERVICE
    if _SERVICE is None:
        ledger_dir = os.environ.get("PSE_LEDGER_DIR", os.path.join("data", "ledger"))
        _SERVICE = PrescriptionService(EventStore(ledger_dir), RuleRegistry())
    return _SERVICE


def health_payload():
    """返回稳定的服务身份信息。"""
    return {"status": "ok", "service": SERVICE_ID, "name": SERVICE_NAME}


def _json_default(obj):
    from datetime import datetime
    if isinstance(obj, datetime):
        return obj.isoformat()
    raise TypeError(f"无法序列化: {type(obj)}")


class Handler(BaseHTTPRequestHandler):
    """HTTP 路由；仅做参数解析与错误映射，业务逻辑全部在门面层。"""

    def _send_json(self, status: int, payload: dict):
        body = json.dumps(payload, ensure_ascii=False, default=_json_default).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            data = json.loads(self.rfile.read(length).decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise ServiceError(f"请求体不是合法 JSON: {exc}", "invalid_json")
        if not isinstance(data, dict):
            raise ServiceError("请求体必须是 JSON 对象")
        return data

    def _error(self, exc: ServiceError):
        self._send_json(exc.status, {"error": exc.code, "message": str(exc)})

    # -------------------------------------------------------------- GET

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        try:
            if path == "/health":
                self._send_json(200, health_payload())
                return
            if path == "/v1/rules":
                self._send_json(200, {"rules": get_service().registry.catalog()})
                return
            parts = [p for p in path.split("/") if p]
            if len(parts) == 4 and parts[:2] == ["v1", "rules"]:
                _, _, ruleset, version = parts
                self._send_json(200, get_service().registry.get(ruleset, version))
                return
            # /v1/cases/<id>/timeline
            if len(parts) == 4 and parts[0] == "v1" and parts[1] == "cases" and parts[3] == "timeline":
                as_of = parse_qs(parsed.query).get("as_of", [None])[0]
                self._send_json(200, get_service().timeline(parts[2], as_of=as_of))
                return
            self._send_json(404, {"error": "not_found", "message": path})
        except ServiceError as exc:
            self._error(exc)
        except FileNotFoundError as exc:
            self._send_json(404, {"error": "not_found", "message": str(exc)})

    # -------------------------------------------------------------- POST

    def do_POST(self):
        parts = [p for p in urlparse(self.path).path.split("/") if p]
        try:
            body = self._read_json()
            svc = get_service()

            if parts == ["v1", "cases"]:
                event = svc.open_case(
                    patient_ref=body["patient_ref"],
                    assessment=body["assessment"],
                    actor_ref=body.get("actor_ref") or body.get("actor") or "unknown",
                    case_id=body.get("case_id"),
                    event_id=body.get("event_id"),
                    at=body.get("at"),
                    timezone_name=body.get("timezone"),
                )
                self._send_json(201, event)
                return

            if len(parts) == 4 and parts[:2] == ["v1", "cases"]:
                case_id = parts[2]
                action = parts[3]
                actor = body.get("actor_ref") or body.get("actor") or "unknown"

                if action == "decisions":
                    self._send_json(201, svc.decide(
                        case_id, actor, event_id=body.get("event_id"), at=body.get("at")))
                    return
                if action == "overrides":
                    self._send_json(201, svc.grant_override(
                        case_id, body["rule_code"], body["reason"], body["authority"],
                        body["valid_until"], actor, valid_from=body.get("valid_from"),
                        event_id=body.get("event_id"), at=body.get("at")))
                    return
                if action == "adjustments":
                    self._send_json(201, svc.adjust_plan(
                        case_id, body["changes"], body["reason"], actor,
                        event_id=body.get("event_id"), at=body.get("at")))
                    return
                if action == "reassessments":
                    result = svc.reassess(
                        case_id, actor, reason=body.get("reason"),
                        event_id=body.get("event_id"), at=body.get("at"))
                    self._send_json(200 if result["event"] is None else 201, result)
                    return
                if action == "resume":
                    self._send_json(201, svc.resume(
                        case_id, body["note"], actor,
                        event_id=body.get("event_id"), at=body.get("at")))
                    return
                if action == "events":
                    self._send_json(201, svc.add_signal(
                        case_id, body["event_type"], body.get("payload", {}), actor,
                        event_time=body["event_time"], event_id=body.get("event_id"),
                        ingested_at=body.get("ingested_at")))
                    return

            self._send_json(404, {"error": "not_found", "message": urlparse(self.path).path})
        except ServiceError as exc:
            self._error(exc)
        except KeyError as exc:
            self._send_json(400, {"error": "missing_field", "message": f"缺少字段: {exc.args[0]}"})

    def log_message(self, *_args):
        return


def main():
    parser = argparse.ArgumentParser(description=SERVICE_NAME)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--ledger-dir", default=os.environ.get("PSE_LEDGER_DIR", "data/ledger"))
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if args.check:
        assert health_payload()["service"] == SERVICE_ID
        # 启动自检：加载并校验全部规则包。
        registry = RuleRegistry()
        catalog = registry.catalog()
        for ruleset, versions in catalog.items():
            assert versions, f"规则集为空: {ruleset}"
            for version in versions:
                pack = registry.get(ruleset, version)
                assert pack["rules_version"] == version
        global _SERVICE
        _SERVICE = PrescriptionService(EventStore(args.ledger_dir), registry)
        print("基础检查通过；规则目录:", json.dumps(catalog, ensure_ascii=False))
        return
    _SERVICE = PrescriptionService(EventStore(args.ledger_dir), RuleRegistry())
    print(f"{SERVICE_NAME} 启动：0.0.0.0:{args.port}，账本目录 {args.ledger_dir}")
    ThreadingHTTPServer(("0.0.0.0", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
