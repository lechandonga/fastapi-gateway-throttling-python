"""限流与配额核心场景测试：并发超限、边界额度、原因区分、规则冲突、失败回滚。"""
import asyncio
import logging

import pytest

from app.throttling.models import DecisionReason, RateLimitRule

log = logging.getLogger("test.throttling")


async def test_concurrent_rate_limit_no_overcount(service, registry):
    """并发 200 个请求打 burst=10 的桶：恰好放行 10 个，其余全部 RATE_LIMITED。"""
    registry.upsert(RateLimitRule("r1", "alice", rate_per_second=0.001,
                                  burst=10, quota_limit=0))
    log.info("input: caller=alice, burst=10, rate=0.001/s, concurrent=200")
    decisions = await asyncio.gather(*[service.check("alice") for _ in range(200)])
    allowed = sum(d.allowed for d in decisions)
    reasons = {d.reason for d in decisions if not d.allowed}
    log.info("basis: allowed=%d, denied_reasons=%s", allowed, {r.value for r in reasons})
    assert allowed == 10
    assert reasons == {DecisionReason.RATE_LIMITED}


async def test_concurrent_quota_no_oversell(service, registry):
    """并发 100 个请求抢 quota=7：恰好 7 个放行，其余 QUOTA_EXHAUSTED，用量精确为 7。"""
    registry.upsert(RateLimitRule("r1", "bob", rate_per_second=1e6,
                                  burst=1000, quota_limit=7))
    log.info("input: caller=bob, quota_limit=7, concurrent=100")
    decisions = await asyncio.gather(*[service.check("bob") for _ in range(100)])
    allowed = sum(d.allowed for d in decisions)
    log.info("basis: allowed=%d, usage=%s", allowed, service._quota.usage("bob"))
    assert allowed == 7
    assert service._quota.usage("bob") == (7, 7)
    assert all(d.reason is DecisionReason.QUOTA_EXHAUSTED
               for d in decisions if not d.allowed)


async def test_boundary_quota_exact(service, registry):
    """边界额度：第 N 个放行、第 N+1 个拒绝，原因必须是 QUOTA_EXHAUSTED 而非 RATE_LIMITED。"""
    registry.upsert(RateLimitRule("r1", "carol", rate_per_second=1e6,
                                  burst=1000, quota_limit=3))
    log.info("input: caller=carol, quota_limit=3, sequential=4")
    decisions = [await service.check("carol") for _ in range(4)]
    for i, d in enumerate(decisions):
        log.info("basis: req#%d allowed=%s reason=%s", i + 1, d.allowed, d.reason.value)
    assert [d.allowed for d in decisions] == [True, True, True, False]
    assert decisions[3].reason is DecisionReason.QUOTA_EXHAUSTED


async def test_reason_distinction_rate_vs_quota(service, registry):
    """同一 caller：先撞速率（RATE_LIMITED），调高速率后撞配额（QUOTA_EXHAUSTED），二者可区分。"""
    registry.upsert(RateLimitRule("r1", "dave", rate_per_second=0.001,
                                  burst=1, quota_limit=100))
    d1 = await service.check("dave")
    d2 = await service.check("dave")
    log.info("basis(rate): d1=%s/%s d2=%s/%s", d1.allowed, d1.reason.value,
             d2.allowed, d2.reason.value)
    assert d1.allowed and d2.reason is DecisionReason.RATE_LIMITED

    registry.upsert(RateLimitRule("r1", "erin", rate_per_second=1e6,
                                  burst=1000, quota_limit=1))
    e1 = await service.check("erin")
    e2 = await service.check("erin")
    log.info("basis(quota): e1=%s/%s e2=%s/%s", e1.allowed, e1.reason.value,
             e2.allowed, e2.reason.value)
    assert e1.allowed and e2.reason is DecisionReason.QUOTA_EXHAUSTED


async def test_rule_conflict_deterministic(service, registry):
    """多条规则同时命中：按 priority > 精确匹配 > rule_id 字典序确定性裁决，重复判定结果一致。"""
    registry.upsert(RateLimitRule("z-wild", "*", 1, 1, 0, priority=1))
    registry.upsert(RateLimitRule("b-exact", "frank", 1, 1, 0, priority=1))
    registry.upsert(RateLimitRule("a-exact", "frank", 1, 1, 0, priority=1))
    log.info("input: rules z-wild(*,p1), b-exact(frank,p1), a-exact(frank,p1)")
    winners = {registry.resolve("frank").rule_id for _ in range(50)}
    log.info("basis: winners over 50 resolves = %s", winners)
    assert winners == {"a-exact"}  # 精确匹配优先于通配；并列取 rule_id 小者

    registry.upsert(RateLimitRule("high", "*", 1, 1, 0, priority=9))
    log.info("input: add high(*,p9)")
    assert registry.resolve("frank").rule_id == "high"  # priority 最高者胜


async def test_dynamic_rule_update_takes_effect(service, registry):
    """运行中调整规则：不重启即生效，判定口径随之变化。"""
    registry.upsert(RateLimitRule("r1", "grace", rate_per_second=0.001,
                                  burst=1, quota_limit=0))
    assert (await service.check("grace")).allowed
    denied = await service.check("grace")
    log.info("basis(before update): denied reason=%s", denied.reason.value)
    assert denied.reason is DecisionReason.RATE_LIMITED

    registry.upsert(RateLimitRule("r1", "grace", rate_per_second=1e6,
                                  burst=100, quota_limit=0))
    after = await service.check("grace")
    log.info("basis(after update): allowed=%s", after.allowed)
    assert after.allowed


async def test_failure_refund_and_no_double_refund(service, registry):
    """失败回滚：预留被退回可再次放行；重复 report_failure 不会重复退额。"""
    registry.upsert(RateLimitRule("r1", "heidi", rate_per_second=1e6,
                                  burst=1000, quota_limit=2))
    assert (await service.check("heidi")).allowed
    assert (await service.check("heidi")).allowed
    blocked = await service.check("heidi")
    log.info("basis: blocked reason=%s usage=%s", blocked.reason.value,
             service._quota.usage("heidi"))
    assert blocked.reason is DecisionReason.QUOTA_EXHAUSTED

    await service.report_failure("heidi")  # 一个请求失败 -> 退 1 额度
    log.info("basis: after refund usage=%s", service._quota.usage("heidi"))
    assert (await service.check("heidi")).allowed

    # 此时有 2 笔预留（最初 2 次放行退了 1 笔后又放行 1 次）
    await service.report_failure("heidi")
    await service.report_failure("heidi")
    await service.report_failure("heidi")  # 无预留可退，应被忽略
    log.info("basis: after extra refunds usage=%s", service._quota.usage("heidi"))
    assert service._quota.usage("heidi") == (0, 2)  # 恰好退 2 笔，不多退


async def test_degraded_mode_passthrough(service, registry):
    """降级路径：放行且不记账，原因标记 DEGRADED，与正常放行可区分。"""
    registry.upsert(RateLimitRule("r1", "ivan", rate_per_second=0.001,
                                  burst=0, quota_limit=0))
    service.set_degraded(True)
    d = await service.check("ivan")
    log.info("basis: degraded decision allowed=%s reason=%s", d.allowed, d.reason.value)
    assert d.allowed and d.reason is DecisionReason.DEGRADED
