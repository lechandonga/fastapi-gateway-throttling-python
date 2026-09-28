"""按调用方维度的令牌桶限流器。"""
from __future__ import annotations

import asyncio
import time


class TokenBucketLimiter:
    """asyncio 并发安全的令牌桶。

    所有对桶状态的读改写都在同一把 asyncio.Lock 内完成，
    同一 caller 并发取令牌时串行化，计数不会漏算或重复扣算。
    配置更新（动态调规则）立即生效：速率/容量变化会保留当前剩余令牌，
    但不超过新容量。
    """

    def __init__(self, clock=time.monotonic) -> None:
        self._clock = clock
        self._tokens: dict[str, float] = {}
        self._last_ts: dict[str, float] = {}
        self._configs: dict[str, tuple[float, int]] = {}  # caller -> (rate, burst)
        self._lock = asyncio.Lock()

    def configure(self, caller: str, rate_per_second: float, burst: int) -> None:
        self._configs[caller] = (rate_per_second, burst)
        # 已有桶的令牌数截断到新容量，避免旧配置遗留的超额令牌
        if caller in self._tokens:
            self._tokens[caller] = min(self._tokens[caller], float(burst))

    def remove(self, caller: str) -> None:
        self._configs.pop(caller, None)
        self._tokens.pop(caller, None)
        self._last_ts.pop(caller, None)

    async def try_acquire(self, caller: str, tokens: int = 1) -> bool:
        async with self._lock:
            rate, burst = self._configs.get(caller, (0.0, 0))
            if rate <= 0 or burst <= 0:
                return False
            now = self._clock()
            last = self._last_ts.get(caller, now)
            available = min(
                float(burst),
                self._tokens.get(caller, float(burst)) + (now - last) * rate,
            )
            self._last_ts[caller] = now
            if available >= tokens:
                self._tokens[caller] = available - tokens
                return True
            self._tokens[caller] = available
            return False
