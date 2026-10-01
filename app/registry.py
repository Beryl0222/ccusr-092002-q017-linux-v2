"""规则注册表：从 rules/<ruleset>/<version>.json 加载并缓存。

历史重放必须使用事件中钉选的规则版本，因此注册表提供 get(version)，
latest() 只用于新开处方，不用于改写历史。
"""

import json
from functools import lru_cache
from pathlib import Path

RULESET_NAMES = ("contraindications", "dosing", "monitoring")


class RuleRegistry:
    def __init__(self, root: Path | str | None = None):
        self.root = Path(root) if root else Path(__file__).resolve().parent.parent / "rules"

    def path_for(self, ruleset: str, version: str) -> Path:
        if ruleset not in RULESET_NAMES:
            raise ValueError(f"未知规则集: {ruleset}")
        # 防止路径穿越。
        if not all(ch.isalnum() or ch in ".-_" for ch in version):
            raise ValueError(f"非法规则版本: {version}")
        return self.root / ruleset / f"{version}.json"

    @lru_cache(maxsize=64)
    def _load(self, ruleset: str, version: str) -> dict:
        path = self.path_for(ruleset, version)
        if not path.exists():
            raise FileNotFoundError(f"规则不存在: {ruleset}@{version}")
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("ruleset") != ruleset or data.get("rules_version") != version:
            raise ValueError(f"规则文件身份不匹配: {path}")
        return data

    def get(self, ruleset: str, version: str) -> dict:
        return self._load(ruleset, version)

    def latest_version(self, ruleset: str) -> str:
        if ruleset not in RULESET_NAMES:
            raise ValueError(f"未知规则集: {ruleset}")
        versions = sorted(p.stem for p in (self.root / ruleset).glob("*.json"))
        if not versions:
            raise FileNotFoundError(f"规则集为空: {ruleset}")
        return versions[-1]

    def latest(self, ruleset: str) -> dict:
        return self.get(ruleset, self.latest_version(ruleset))

    def catalog(self) -> dict:
        return {
            name: sorted(p.stem for p in (self.root / name).glob("*.json"))
            for name in RULESET_NAMES
        }
