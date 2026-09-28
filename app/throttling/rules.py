"""限流规则注册表：运行时动态增删改，命中判定确定性。"""
from __future__ import annotations

import threading

from .models import RateLimitRule


class RuleRegistry:
    """规则表，upsert/remove 立即生效，无需重启。

    多条规则同时命中时的确定性裁决（不随机）：
      1. priority 数值大者优先；
      2. 并列时，精确匹配 caller 的规则优先于通配 "*" 规则；
      3. 再并列时，rule_id 字典序小者优先。
    因此同一 caller 在规则不变时永远得到同一条规则，判定可解释、可复现。
    """

    def __init__(self) -> None:
        self._rules: dict[str, RateLimitRule] = {}
        self._lock = threading.Lock()

    def upsert(self, rule: RateLimitRule) -> None:
        with self._lock:
            self._rules[rule.rule_id] = rule

    def remove(self, rule_id: str) -> bool:
        with self._lock:
            return self._rules.pop(rule_id, None) is not None

    def list_rules(self) -> list[RateLimitRule]:
        with self._lock:
            return sorted(self._rules.values(), key=lambda r: r.rule_id)

    @staticmethod
    def _sort_key(rule: RateLimitRule) -> tuple[int, int, str]:
        specificity = 1 if rule.caller_pattern != "*" else 0
        # 取负/反向以便用 min() 选出最优：priority 大、specificity 大、rule_id 小
        return (-rule.priority, -specificity, rule.rule_id)

    def resolve(self, caller: str) -> RateLimitRule | None:
        """返回命中规则中确定性最优的一条；无命中返回 None。"""
        with self._lock:
            candidates = [
                r for r in self._rules.values()
                if r.caller_pattern == caller or r.caller_pattern == "*"
            ]
        if not candidates:
            return None
        return min(candidates, key=self._sort_key)
