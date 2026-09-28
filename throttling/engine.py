"""限流判定引擎。

职责：
1. 取规则快照，按调用方/资源筛选命中的速率与配额规则；
2. 确定性地选出每条规则链上的最终生效规则（具体度优先，再按 priority、id 兜底）；
3. 在锁内原子完成「速率名额 + 配额额度」的扣减——任一不满足则整体不扣减；
4. 返回带完整判定依据 RuleVerdict 的 DecisionResult，区分
   RATE_LIMITED 与 QUOTA_EXHAUSTED；
5. 通过 Lease 支持请求结束后的 commit（成功/失败结算）与 rollback（降级回滚）。
"""

from __future__ import annotations

import logging
import threading
import time

from .models import (
    Decision,
    DecisionResult,
    Lease,
    LimitScope,
    QuotaRule,
    RateRule,
    RuleVerdict,
)
from .registry import RuleRegistry
from .store import ThrottleStore

logger = logging.getLogger("throttling")

_WILDCARD = "*"


def _rate_key(rule_id: str, caller_id: str, resource: str, scope: LimitScope) -> str:
    if scope is LimitScope.GLOBAL:
        return f"rate:{rule_id}:{_WILDCARD}"
    if scope is LimitScope.CALLER_RESOURCE:
        return f"rate:{rule_id}:{caller_id}:{resource}"
    return f"rate:{rule_id}:{caller_id}"


def _quota_key(rule_id: str, caller_id: str, resource: str, scope: LimitScope) -> str:
    if scope is LimitScope.GLOBAL:
        return f"quota:{rule_id}:{_WILDCARD}"
    if scope is LimitScope.CALLER_RESOURCE:
        return f"quota:{rule_id}:{caller_id}:{resource}"
    return f"quota:{rule_id}:{caller_id}"


def _matches(scope: LimitScope, rule_resource: str | None, caller_id: str, resource: str) -> bool:
    if scope is LimitScope.GLOBAL:
        return rule_resource is None or rule_resource in (_WILDCARD, resource)
    if scope is LimitScope.CALLER_RESOURCE:
        return rule_resource in (None, _WILDCARD, resource)
    # CALLER 维度规则不区分资源，对该调用方的所有资源生效
    return True


def _specificity(scope: LimitScope, rule_resource: str | None) -> tuple:
    """具体度排序键：值越大越具体、越优先。"""
    exact_resource = 1 if rule_resource not in (None, _WILDCARD) else 0
    scope_rank = {
        LimitScope.CALLER_RESOURCE: 2,
        LimitScope.CALLER: 1,
        LimitScope.GLOBAL: 0,
    }[scope]
    return (scope_rank, exact_resource)


class ThrottleEngine:
    def __init__(self, registry: RuleRegistry, store: ThrottleStore) -> None:
        self._registry = registry
        self._store = store
        # 引擎锁串行化一次完整的「选规则 + 双计数扣减」，保证与结算互斥
        self._lock = threading.RLock()
        self._rules_version = -1

    def _sync_store_with_registry(self) -> None:
        """检测规则版本变化并清理失效计数键（调用方须持锁）。"""
        version = self._registry.version
        if version == self._rules_version:
            return
        rate_snapshot, quota_snapshot = self._registry.snapshot()
        self._store.prune(
            {r.id for r in rate_snapshot},
            {q.id for q in quota_snapshot},
        )
        self._rules_version = version

    # ------------------------------------------------------------------ #
    # 规则选择（确定性）
    # ------------------------------------------------------------------ #
    def _select_rules(
        self, caller_id: str, resource: str
    ) -> tuple[list[RateRule], list[QuotaRule]]:
        rate_snapshot, quota_snapshot = self._registry.snapshot()
        rate_rules = [
            r for r in rate_snapshot if _matches(r.scope, r.resource, caller_id, resource)
        ]
        quota_rules = [
            r for r in quota_snapshot if _matches(r.scope, r.resource, caller_id, resource)
        ]

        # 具体度 -> priority（大者优先）-> id 字典序兜底，保证结果唯一且可解释
        rate_rules.sort(key=lambda r: (*_specificity(r.scope, r.resource), r.priority, _neg_str(r.id)), reverse=True)
        quota_rules.sort(key=lambda r: (*_specificity(r.scope, r.resource), r.priority, _neg_str(r.id)), reverse=True)
        return rate_rules, quota_rules

    # ------------------------------------------------------------------ #
    # 准入判定
    # ------------------------------------------------------------------ #
    def _ensure_quotas(self, quota_rules: list[QuotaRule], caller_id: str, resource: str) -> None:
        """确保每条配额规则的计数已按当前配置初始化（热更新后立即生效）。"""
        for qr in quota_rules:
            qkey = _quota_key(qr.id, caller_id, resource, qr.scope)
            if self._store.quota_limit(qkey) != qr.quota:
                self._store.configure_quota(qkey, qr.quota)

    def decide(self, caller_id: str, resource: str, now: float | None = None) -> DecisionResult:
        if now is None:
            now = time.monotonic()
        with self._lock:
            self._sync_store_with_registry()
            rate_rules, quota_rules = self._select_rules(caller_id, resource)

            self._ensure_quotas(quota_rules, caller_id, resource)

            verdicts: list[RuleVerdict] = []
            rate_blocker: RateRule | None = None
            quota_blocker: QuotaRule | None = None
            rate_used = 0
            rate_limit = 0
            quota_used = 0
            quota_limit = 0

            for rr in rate_rules:
                rkey = _rate_key(rr.id, caller_id, resource, rr.scope)
                used = self._store.peek_rate(rkey, now, rr.time_window_seconds)
                allowed = used < rr.limit
                verdicts.append(
                    RuleVerdict(
                        rule_id=rr.id,
                        kind="rate",
                        allowed=allowed,
                        used=used,
                        limit=rr.limit,
                        reason=(
                            f"{used}/{rr.limit} used in {rr.time_window_seconds}s window"
                            if allowed
                            else f"rate limit {rr.limit} reached in {rr.time_window_seconds}s window"
                        ),
                    )
                )
                if not allowed and rate_blocker is None:
                    rate_blocker = rr
                    rate_used, rate_limit = used, rr.limit

            for qr in quota_rules:
                qkey = _quota_key(qr.id, caller_id, resource, qr.scope)
                limit = self._store.quota_limit(qkey)
                remaining = self._store.remaining_quota(qkey)
                used = limit - remaining
                allowed = remaining >= 1
                verdicts.append(
                    RuleVerdict(
                        rule_id=qr.id,
                        kind="quota",
                        allowed=allowed,
                        used=used,
                        limit=limit,
                        reason=(
                            f"{remaining}/{limit} quota remaining"
                            if allowed
                            else f"quota {limit} exhausted"
                        ),
                    )
                )
                if not allowed and quota_blocker is None:
                    quota_blocker = qr
                    quota_used, quota_limit = used, limit

            # 冲突/同时命中时的确定顺序：先判配额耗尽，再判速率超限。
            # 速率名额随窗口自动恢复，配额是硬上限，因此优先报告配额。
            if quota_blocker is not None:
                decision = Decision.QUOTA_EXHAUSTED
            elif rate_blocker is not None:
                decision = Decision.RATE_LIMITED
            else:
                decision = Decision.ALLOW

            lease: Lease | None = None
            if decision is Decision.ALLOW:
                # 原子扣减：速率与配额都在本次锁内扣。
                acquired_rate: list[RateRule] = []
                for rr in rate_rules:
                    rkey = _rate_key(rr.id, caller_id, resource, rr.scope)
                    if self._store.try_acquire_rate(
                        rkey, rr.limit, rr.time_window_seconds, now
                    ):
                        acquired_rate.append(rr)
                    else:
                        decision = Decision.RATE_LIMITED
                        break
                acquired_quota: list[QuotaRule] = []
                if decision is Decision.ALLOW:
                    for qr in quota_rules:
                        qkey = _quota_key(qr.id, caller_id, resource, qr.scope)
                        if self._store.try_acquire_quota(qkey):
                            acquired_quota.append(qr)
                        else:
                            decision = Decision.QUOTA_EXHAUSTED
                            break
                if decision is Decision.ALLOW:
                    lease = Lease(
                        caller_id=caller_id,
                        resource=resource,
                        rate_rule_id=rate_rules[0].id if rate_rules else None,
                        quota_rule_id=quota_rules[0].id if quota_rules else None,
                        rate_scope=rate_rules[0].scope if rate_rules else None,
                        quota_scope=quota_rules[0].scope if quota_rules else None,
                        charge_on_failure=quota_rules[0].charge_on_failure if quota_rules else False,
                    )
                else:
                    # 任一扣减失败：回滚本次已扣的全部计数，绝不漏/重扣
                    for qr in acquired_quota:
                        qkey = _quota_key(qr.id, caller_id, resource, qr.scope)
                        self._store.release_quota(qkey)
                    self._compensate(caller_id, resource, acquired_rate, now)

            result = DecisionResult(
                decision=decision,
                caller_id=caller_id,
                resource=resource,
                verdicts=tuple(verdicts),
                lease=lease,
            )
            logger.info(
                "throttle decide %s",
                result.explain().replace("\n", " | "),
            )
            return result

    def _compensate(
        self,
        caller_id: str,
        resource: str,
        acquired_rate: list[RateRule],
        now: float,
    ) -> None:
        """扣减阶段部分成功时的补偿：精确移除本次写入的速率时间戳。"""
        for rr in acquired_rate:
            rkey = _rate_key(rr.id, caller_id, resource, rr.scope)
            self._store.pop_last_rate(rkey, now)

    # ------------------------------------------------------------------ #
    # 结算：commit / rollback
    # ------------------------------------------------------------------ #
    def commit(self, lease: Lease, success: bool) -> None:
        """请求处理结束后的结算。

        - success=True：额度已在准入时扣减，速率名额始终保留（请求确已进入）；
        - success=False：按生效配额规则的 charge_on_failure 决定保留还是退还；
        - 重复结算被幂等忽略。
        """
        with self._lock:
            if lease.committed or lease.rolled_back:
                logger.info("commit ignored (lease already settled): %s", lease)
                return
            if lease.quota_rule_id is not None and not success and not lease.charge_on_failure:
                qkey = _quota_key(
                    lease.quota_rule_id, lease.caller_id, lease.resource,
                    lease.quota_scope or LimitScope.CALLER,
                )
                self._store.release_quota(qkey)
                logger.info(
                    "quota released on failure caller=%s resource=%s rule=%s",
                    lease.caller_id, lease.resource, lease.quota_rule_id,
                )
            lease.committed = True
            logger.info(
                "lease committed caller=%s resource=%s success=%s",
                lease.caller_id, lease.resource, success,
            )

    def rollback(self, lease: Lease) -> None:
        """降级路径：退还配额（速率名额代表「已发生的请求」，按约定不退还）。"""
        with self._lock:
            if lease.committed or lease.rolled_back:
                logger.info("rollback ignored (lease already settled): %s", lease)
                return
            if lease.quota_rule_id is not None:
                qkey = _quota_key(
                    lease.quota_rule_id, lease.caller_id, lease.resource,
                    lease.quota_scope or LimitScope.CALLER,
                )
                self._store.release_quota(qkey)
            lease.rolled_back = True
            logger.info(
                "lease rolled back (degraded path) caller=%s resource=%s",
                lease.caller_id, lease.resource,
            )

    # ------------------------------------------------------------------ #
    # 状态视图
    # ------------------------------------------------------------------ #
    def status(self, caller_id: str, resource: str, now: float | None = None) -> list[dict]:
        if now is None:
            now = time.monotonic()
        with self._lock:
            self._sync_store_with_registry()
            rate_rules, quota_rules = self._select_rules(caller_id, resource)
            self._ensure_quotas(quota_rules, caller_id, resource)
            view: list[dict] = []
            for rr in rate_rules:
                rkey = _rate_key(rr.id, caller_id, resource, rr.scope)
                used = self._store.peek_rate(rkey, now, rr.time_window_seconds)
                view.append(
                    {
                        "rule_id": rr.id,
                        "kind": "rate",
                        "limit": rr.limit,
                        "used": used,
                        "remaining": max(0, rr.limit - used),
                        "window_seconds": rr.time_window_seconds,
                    }
                )
            for qr in quota_rules:
                qkey = _quota_key(qr.id, caller_id, resource, qr.scope)
                limit = self._store.quota_limit(qkey)
                remaining = self._store.remaining_quota(qkey)
                view.append(
                    {
                        "rule_id": qr.id,
                        "kind": "quota",
                        "limit": limit,
                        "used": limit - remaining,
                        "remaining": remaining,
                        "charge_on_failure": qr.charge_on_failure,
                    }
                )
            return view


def _neg_str(s: str) -> tuple:
    """把字符串转成可用于 reverse 排序的「取反」键，使 id 字典序较小者优先。"""
    return tuple(-ord(ch) for ch in s)
