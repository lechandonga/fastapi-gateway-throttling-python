"""限流判定服务：编排规则解析、限流、配额与降级路径。"""
from __future__ import annotations

import asyncio

from .limiter import TokenBucketLimiter
from .models import Decision, DecisionReason
from .quota import QuotaManager
from .rules import RuleRegistry


class ThrottleService:
    """统一判定入口，所有调用入口（HTTP、内部调用等）共用一个实例，结论一致。

    判定顺序（固定、可解释）：
      1. 降级模式：直接放行，不消耗速率令牌与配额（约定：降级不记账，无需回滚）；
      2. 无命中规则：放行（默认开放），不记账；
      3. 速率检查：超限则拒绝，原因 RATE_LIMITED，不扣配额；
      4. 配额预留：耗尽则拒绝，原因 QUOTA_EXHAUSTED；
      5. 放行：配额处于"已预留"状态，等待 report_success / report_failure。

    回滚约定：report_success 提交预留；report_failure（含下游异常、超时）
    把预留额度退回。只有处于预留状态的请求才能回滚，避免重复退额。
    """

    def __init__(self, registry: RuleRegistry, limiter: TokenBucketLimiter,
                 quota: QuotaManager, degraded: bool = False) -> None:
        self._registry = registry
        self._limiter = limiter
        self._quota = quota
        self._degraded = degraded
        self._pending: dict[str, int] = {}  # caller -> 已预留未结算的请求数
        self._settle_lock = asyncio.Lock()

    def set_degraded(self, degraded: bool) -> None:
        self._degraded = degraded

    async def check(self, caller: str) -> Decision:
        if self._degraded:
            return Decision(True, DecisionReason.DEGRADED,
                            detail="degraded mode: pass through, no accounting")

        rule = self._registry.resolve(caller)
        if rule is None:
            return Decision(True, DecisionReason.ALLOWED,
                            detail="no rule matched, default allow")

        # 动态规则即时生效：把最新规则参数同步到限流器与配额表
        self._limiter.configure(caller, rule.rate_per_second, rule.burst)
        self._quota.set_limit(caller, rule.quota_limit)

        if not await self._limiter.try_acquire(caller):
            return Decision(False, DecisionReason.RATE_LIMITED, rule.rule_id,
                            f"rate {rule.rate_per_second}/s burst {rule.burst} exceeded")

        if not await self._quota.reserve(caller):
            return Decision(False, DecisionReason.QUOTA_EXHAUSTED, rule.rule_id,
                            f"quota limit {rule.quota_limit} exhausted")

        async with self._settle_lock:
            self._pending[caller] = self._pending.get(caller, 0) + 1
        return Decision(True, DecisionReason.ALLOWED, rule.rule_id,
                        "within rate and quota, quota reserved")

    async def report_success(self, caller: str) -> None:
        """请求成功：提交预留配额（预留已计入用量，此处仅结算状态）。"""
        async with self._settle_lock:
            if self._pending.get(caller, 0) > 0:
                self._pending[caller] -= 1
        await self._quota.commit(caller)

    async def report_failure(self, caller: str) -> None:
        """请求失败或降级：按约定回滚已预留配额；无预留则忽略，防止重复退额。"""
        async with self._settle_lock:
            if self._pending.get(caller, 0) <= 0:
                return
            self._pending[caller] -= 1
        await self._quota.refund(caller)
