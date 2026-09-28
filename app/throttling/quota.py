"""按调用方维度的配额管理：预留/提交/回滚两阶段模型。"""
from __future__ import annotations

import asyncio


class QuotaManager:
    """配额两阶段扣减：reserve -> commit / refund。

    - reserve：在锁内检查并立即计入 used，保证并发下不会超卖；
    - commit：请求成功，预留转为正式消耗（本实现中 reserve 已计入，commit 为幂等确认）；
    - refund：请求失败或走降级，按约定把预留额度退回。
    所有状态变更都在同一把 asyncio.Lock 内，计数不会漏算或重复扣算。
    limit 为 0 表示不限制配额。
    """

    def __init__(self) -> None:
        self._limits: dict[str, int] = {}
        self._used: dict[str, int] = {}
        self._lock = asyncio.Lock()

    def set_limit(self, caller: str, limit: int) -> None:
        self._limits[caller] = limit
        self._used.setdefault(caller, 0)

    def remove(self, caller: str) -> None:
        self._limits.pop(caller, None)
        self._used.pop(caller, None)

    async def reserve(self, caller: str, amount: int = 1) -> bool:
        async with self._lock:
            limit = self._limits.get(caller, 0)
            if limit == 0:
                return True  # 不限制配额，无需记账
            used = self._used.get(caller, 0)
            if used + amount > limit:
                return False
            self._used[caller] = used + amount
            return True

    async def commit(self, caller: str, amount: int = 1) -> None:
        # reserve 已计入 used，commit 无需再改数；保留接口以明确语义。
        async with self._lock:
            self._used.setdefault(caller, 0)

    async def refund(self, caller: str, amount: int = 1) -> None:
        async with self._lock:
            limit = self._limits.get(caller, 0)
            if limit == 0:
                return
            self._used[caller] = max(0, self._used.get(caller, 0) - amount)

    def usage(self, caller: str) -> tuple[int, int]:
        """返回 (已用, 上限)；上限 0 表示不限制。"""
        return self._used.get(caller, 0), self._limits.get(caller, 0)
