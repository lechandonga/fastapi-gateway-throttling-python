"""规则注册表：规则的增删改查与原子快照，支持不重启热更新。

引擎每次判定只通过 snapshot() 取一份不可变快照，更新动作在锁内完成，
因此「读到的整套规则」必然是某个时刻的一致版本，不会看到半更新状态。
"""

from __future__ import annotations

import threading

from .models import QuotaRule, RateRule


class RuleRegistry:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._rate_rules: dict[str, RateRule] = {}
        self._quota_rules: dict[str, QuotaRule] = {}
        self._version: int = 0

    @property
    def version(self) -> int:
        with self._lock:
            return self._version

    def add_rate_rule(self, rule: RateRule) -> None:
        """新增或按 id 覆盖一条速率规则。"""
        with self._lock:
            self._rate_rules[rule.id] = rule
            self._version += 1

    def add_quota_rule(self, rule: QuotaRule) -> None:
        """新增或按 id 覆盖一条配额规则。"""
        with self._lock:
            self._quota_rules[rule.id] = rule
            self._version += 1

    def remove_rule(self, rule_id: str) -> bool:
        with self._lock:
            existed = self._rate_rules.pop(rule_id, None) is not None
            existed = self._quota_rules.pop(rule_id, None) is not None or existed
            if existed:
                self._version += 1
            return existed

    def replace_all(
        self,
        rate_rules: list[RateRule],
        quota_rules: list[QuotaRule],
    ) -> None:
        """原子地整体替换规则（配置文件重载时使用）。"""
        rate_map: dict[str, RateRule] = {}
        for rule in rate_rules:
            if rule.id in rate_map:
                raise ValueError(f"duplicate rate rule id: {rule.id}")
            rate_map[rule.id] = rule
        quota_map: dict[str, QuotaRule] = {}
        for rule in quota_rules:
            if rule.id in quota_map:
                raise ValueError(f"duplicate quota rule id: {rule.id}")
            quota_map[rule.id] = rule
        with self._lock:
            self._rate_rules = rate_map
            self._quota_rules = quota_map
            self._version += 1

    def snapshot(self) -> tuple[tuple[RateRule, ...], tuple[QuotaRule, ...]]:
        """返回当前规则的不可变快照（元组）。"""
        with self._lock:
            return tuple(self._rate_rules.values()), tuple(self._quota_rules.values())
