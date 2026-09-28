"""规则配置加载：从 JSON 字典/文件装载规则并做字段校验。

配置示例见 docs/throttling.md 与 rules.example.json。
"""

from __future__ import annotations

import json

from .models import LimitScope, QuotaRule, RateRule
from .registry import RuleRegistry


def _as_scope(value: object, rule_id: str) -> LimitScope:
    try:
        return LimitScope(value)
    except ValueError as exc:
        raise ValueError(
            f"rule {rule_id!r}: invalid scope {value!r}, "
            f"expected one of {[s.value for s in LimitScope]}"
        ) from exc


def _build_rate_rule(raw: dict) -> RateRule:
    if "id" not in raw:
        raise ValueError("rate rule missing required field 'id'")
    rule_id = str(raw["id"])
    limit = int(raw.get("limit", -1))
    window = float(raw.get("time_window_seconds", -1))
    if limit <= 0:
        raise ValueError(f"rate rule {rule_id!r}: limit must be > 0")
    if window <= 0:
        raise ValueError(f"rate rule {rule_id!r}: time_window_seconds must be > 0")
    return RateRule(
        id=rule_id,
        limit=limit,
        time_window_seconds=window,
        scope=_as_scope(raw.get("scope", LimitScope.CALLER.value), rule_id),
        resource=raw.get("resource"),
        priority=int(raw.get("priority", 0)),
        description=str(raw.get("description", "")),
    )


def _build_quota_rule(raw: dict) -> QuotaRule:
    if "id" not in raw:
        raise ValueError("quota rule missing required field 'id'")
    rule_id = str(raw["id"])
    quota = int(raw.get("quota", -1))
    if quota < 0:
        raise ValueError(f"quota rule {rule_id!r}: quota must be >= 0")
    return QuotaRule(
        id=rule_id,
        quota=quota,
        scope=_as_scope(raw.get("scope", LimitScope.CALLER.value), rule_id),
        resource=raw.get("resource"),
        priority=int(raw.get("priority", 0)),
        charge_on_failure=bool(raw.get("charge_on_failure", False)),
        description=str(raw.get("description", "")),
    )


def load_rules_from_dict(payload: dict) -> tuple[list[RateRule], list[QuotaRule]]:
    """解析 JSON 兼容字典，返回速率规则与配额规则列表。"""
    if not isinstance(payload, dict):
        raise ValueError("config payload must be an object")
    rate_rules = [_build_rate_rule(item) for item in payload.get("rate_rules", [])]
    quota_rules = [_build_quota_rule(item) for item in payload.get("quota_rules", [])]
    return rate_rules, quota_rules


def load_rules_from_file(path: str) -> tuple[list[RateRule], list[QuotaRule]]:
    with open(path, "r", encoding="utf-8") as fh:
        payload = json.load(fh)
    return load_rules_from_dict(payload)


def apply_config(registry: RuleRegistry, payload: dict) -> None:
    """把配置字典原子地应用到注册表。"""
    rate_rules, quota_rules = load_rules_from_dict(payload)
    registry.replace_all(rate_rules, quota_rules)


def apply_config_file(registry: RuleRegistry, path: str) -> None:
    """从文件加载配置并原子地应用到注册表。"""
    rate_rules, quota_rules = load_rules_from_file(path)
    registry.replace_all(rate_rules, quota_rules)
