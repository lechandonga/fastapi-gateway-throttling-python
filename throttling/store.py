"""并发安全的计数存储：固定窗口式速率计数 + 配额余额。

所有读改写操作都在同一把锁内原子完成，保证多线程并发下
计数既不漏算（少放行）也不重复扣算（多扣额度）。
"""

from __future__ import annotations

from collections import defaultdict, deque
from threading import Lock


class ThrottleStore:
    """内存计数存储。

    - 速率计数：每个 key 维护一个时间戳双端队列，落在窗口内的请求时间戳
      即当前窗口已用次数；try_acquire 在锁内完成「清理过期 + 判额 + 记账」。
    - 配额余额：每个 key 维护 (总额度, 剩余额度)。
    """

    def __init__(self) -> None:
        self._lock = Lock()
        self._rate_hits: dict[str, deque[float]] = defaultdict(deque)
        # key -> {"limit": int, "remaining": int, "consumed": int}
        self._quotas: dict[str, dict[str, int]] = {}

    def _evict(self, hits: deque[float], now: float, window_seconds: float) -> None:
        boundary = now - window_seconds
        while hits and hits[0] <= boundary:
            hits.popleft()

    def try_acquire_rate(self, key: str, limit: int, window_seconds: float, now: float) -> bool:
        """原子地尝试在窗口内占用一个速率名额。"""
        with self._lock:
            hits = self._rate_hits[key]
            self._evict(hits, now, window_seconds)
            if len(hits) >= limit:
                return False
            hits.append(now)
            return True

    def peek_rate(self, key: str, now: float, window_seconds: float | None = None) -> int:
        """查看窗口内已用次数（不扣减）。window_seconds 为 None 时返回原始记录数。"""
        with self._lock:
            hits = self._rate_hits.get(key)
            if hits is None:
                return 0
            if window_seconds is not None:
                self._evict(hits, now, window_seconds)
            return len(hits)

    def reset_rate(self, key: str) -> None:
        with self._lock:
            self._rate_hits.pop(key, None)

    def pop_last_rate(self, key: str, expected: float) -> bool:
        """原子地移除队尾一次指定时间戳的速率记账（准入补偿用）。"""
        with self._lock:
            hits = self._rate_hits.get(key)
            if hits and hits[-1] == expected:
                hits.pop()
                return True
            return False

    def reset_quota(self, key: str) -> None:
        """将配额余额重置为总额度（新计费周期），累计消耗清零。"""
        with self._lock:
            entry = self._quotas.get(key)
            if entry is not None:
                entry["remaining"] = entry["limit"]
                entry["consumed"] = 0

    def configure_quota(self, key: str, quota: int) -> None:
        """设置/调整总额度。

        动态调整只改 limit，保留独立的累计 consumed：
        remaining = max(0, quota - consumed)。
        连续多次调大调小结果都确定：累计消耗不被重算。
        """
        if quota < 0:
            raise ValueError("quota must be >= 0")
        with self._lock:
            entry = self._quotas.get(key)
            if entry is None:
                self._quotas[key] = {"limit": quota, "remaining": quota, "consumed": 0}
            else:
                entry["limit"] = quota
                entry["remaining"] = max(0, quota - entry["consumed"])

    def try_acquire_quota(self, key: str, amount: int = 1) -> bool:
        """原子地尝试扣减配额。"""
        if amount <= 0:
            raise ValueError("amount must be > 0")
        with self._lock:
            entry = self._quotas.get(key)
            if entry is None or entry["remaining"] < amount:
                return False
            entry["remaining"] -= amount
            entry["consumed"] += amount
            return True

    def release_quota(self, key: str, amount: int = 1) -> None:
        """退还配额（回滚），余额不会超过总额度。"""
        with self._lock:
            entry = self._quotas.get(key)
            if entry is None:
                return
            refunded = min(amount, entry["consumed"])
            entry["consumed"] -= refunded
            entry["remaining"] = min(entry["limit"], entry["remaining"] + refunded)

    def remaining_quota(self, key: str) -> int:
        with self._lock:
            entry = self._quotas.get(key)
            return entry["remaining"] if entry is not None else 0

    def quota_limit(self, key: str) -> int:
        with self._lock:
            entry = self._quotas.get(key)
            return entry["limit"] if entry is not None else 0

    def snapshot(self) -> dict:
        """只读快照，供状态查询/测试断言。"""
        with self._lock:
            return {
                "rate_keys": {k: list(v) for k, v in self._rate_hits.items()},
                "quota_keys": {k: dict(v) for k, v in self._quotas.items()},
            }

    def prune(self, live_rate_rule_ids: set[str], live_quota_rule_ids: set[str]) -> None:
        """规则热更新后清理失效计数键。

        key 形如 "rate:{ruleId}:..." / "quota:{ruleId}:..."，
        按第二段（规则 id）判断是否还属于现行规则集。
        """

        def rule_id_of(key: str) -> str:
            parts = key.split(":", 2)
            return parts[1] if len(parts) >= 2 else ""

        with self._lock:
            for key in list(self._rate_hits.keys()):
                if rule_id_of(key) not in live_rate_rule_ids:
                    self._rate_hits.pop(key, None)
            for key in list(self._quotas.keys()):
                if rule_id_of(key) not in live_quota_rule_ids:
                    self._quotas.pop(key, None)
