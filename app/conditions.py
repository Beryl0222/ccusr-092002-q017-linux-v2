"""规则条件求值器。

规则 JSON 中的 when 节点：
  {"op": "and"/"all", "nodes": [...]}   或 {"all": [...]}
  {"op": "any"/"or",  "nodes": [...]}   或 {"any": [...]}
  {"op": "not", "node": {...}}
  {"op": "missing", "path": "a.b"}
  {"op": "==", "path": "a.b", "value": x}
  {"op": "!=", ">", ">=", "<", "<=", "path": ..., "value": ...}
  {"op": "contains", "path": "a.b", "value": x}
  {"op": "contains_any", "path": "a.b", "value": [x, y]}

失败安全原则：路径缺失时所有比较/包含判断一律为 False；
只有 missing 操作符能对缺失作出肯定判断。
"""

from typing import Any

MISSING = object()


def get_path(ctx: Any, path: str) -> Any:
    cur = ctx
    for part in path.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return MISSING
    return cur


def _as_children(node: dict) -> list:
    if "nodes" in node:
        return node["nodes"]
    for key in ("all", "any"):
        if key in node:
            return node[key]
    raise ValueError(f"逻辑节点缺少子节点: {node}")


def evaluate(node: dict, ctx: Any) -> bool:
    op = node.get("op")
    if op is None:
        # 兼容 {"all": [...]} / {"any": [...]} 省略 op 的写法。
        if "all" in node:
            return all(evaluate(c, ctx) for c in node["all"])
        if "any" in node:
            return any(evaluate(c, ctx) for c in node["any"])
        raise ValueError(f"条件节点缺少 op: {node}")
    if op in ("and", "all"):
        return all(evaluate(c, ctx) for c in _as_children(node))
    if op in ("or", "any"):
        return any(evaluate(c, ctx) for c in _as_children(node))
    if op == "not":
        return not evaluate(node["node"], ctx)
    if op == "missing":
        # 结构缺失或显式 null 都视为信息缺失（失败安全）。
        actual = get_path(ctx, node["path"])
        return actual is MISSING or actual is None

    actual = get_path(ctx, node["path"])
    if actual is MISSING or actual is None:
        # 缺失或显式 null 都不参与肯定判断（== null 例外，不允许使用）。
        return False

    if op == "==":
        return actual == node["value"]
    if op == "!=":
        return actual != node["value"]
    if op == ">":
        return actual > node["value"]
    if op == ">=":
        return actual >= node["value"]
    if op == "<":
        return actual < node["value"]
    if op == "<=":
        return actual <= node["value"]
    if op == "contains":
        try:
            return node["value"] in actual
        except TypeError:
            return False
    if op == "contains_any":
        try:
            return any(v in actual for v in node["value"])
        except TypeError:
            return False
    raise ValueError(f"未知操作符: {op}")
