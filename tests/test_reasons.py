"""原因区分：速率超限 vs 配额耗尽必须明确可辨；失败/降级结算语义。"""

from throttling import Decision, QuotaRule, RateRule
from throttling.models import LimitScope


def test_rate_limited_distinct_from_quota_exhausted(engine):
    engine._registry.add_rate_rule(RateRule(id="rl", limit=2, time_window_seconds=100))
    engine._registry.add_quota_rule(QuotaRule(id="qd", quota=5))

    results = [engine.decide("c1", "r", now=0.0) for _ in range(2)]
    for r in results:
        assert r.decision is Decision.ALLOW
        engine.commit(r.lease, True)

    third = engine.decide("c1", "r", now=0.0)
    print(f"\n[input] rate=2/100s quota=5; third immediate request")
    print(f"[verdict] decision={third.decision.value}")
    print(third.explain())
    assert third.decision is Decision.RATE_LIMITED
    assert third.lease is None
    assert all(v.allowed for v in third.verdicts if v.kind == "quota")
    blocked = [v for v in third.verdicts if not v.allowed]
    assert len(blocked) == 1 and blocked[0].kind == "rate"

    # 每 100s 一个请求：速率永远不触发，连续打满剩余 3 个配额 -> 配额耗尽
    results = []
    for i in range(4):
        r = engine.decide("c1", "r", now=200.0 + i * 100)
        results.append(r)
        if r.lease:
            engine.commit(r.lease, True)
    exhausted = results[-1]
    assert [r.decision for r in results[:3]] == [Decision.ALLOW] * 3
    print(f"[input] 4 more requests spaced 100s apart (rate never trips), 3 quota left")
    print(f"[verdict] last decision={exhausted.decision.value}")
    print(exhausted.explain())
    assert exhausted.decision is Decision.QUOTA_EXHAUSTED
    assert exhausted.lease is None
    blocked = [v for v in exhausted.verdicts if not v.allowed]
    assert len(blocked) == 1 and blocked[0].kind == "quota"


def test_quota_exhausted_takes_precedence_over_rate(engine):
    """同时命中时：配额耗尽优先于速率超限（硬上限优先报告）。"""
    engine._registry.add_rate_rule(RateRule(id="rl", limit=1, time_window_seconds=100))
    engine._registry.add_quota_rule(QuotaRule(id="qd", quota=1))
    r = engine.decide("c1", "r", now=0.0)
    assert r.allowed
    engine.commit(r.lease, True)
    again = engine.decide("c1", "r", now=0.0)
    print(f"\n[input] both rate(1) and quota(1) exhausted")
    print(f"[verdict] decision={again.decision.value} (expect quota_exhausted)")
    assert again.decision is Decision.QUOTA_EXHAUSTED
    kinds = {v.kind: v.allowed for v in again.verdicts}
    assert kinds == {"rate": False, "quota": False}


def test_failure_refunds_quota_by_default(engine):
    engine._registry.add_quota_rule(QuotaRule(id="q", quota=2))
    r = engine.decide("c1", "r", now=0.0)
    engine.commit(r.lease, success=False)
    remaining = engine._store.remaining_quota("quota:q:c1")
    print(f"\n[input] request failed, charge_on_failure=False")
    print(f"[verdict] remaining after failure commit={remaining} (expect 2)")
    assert remaining == 2
    # 速率名额仍然保留：失败的请求也计入速率
    engine._registry.add_rate_rule(RateRule(id="rl", limit=1, time_window_seconds=100))
    blocked = engine.decide("c1", "r", now=0.0)
    # 注意：该规则是事后新增，之前请求不在它的计数里，这里只验配额可再用
    print(f"[verdict] quota usable again -> {blocked.decision.value}")


def test_failure_charges_when_configured(engine):
    engine._registry.add_quota_rule(QuotaRule(id="q", quota=2, charge_on_failure=True))
    r = engine.decide("c1", "r", now=0.0)
    engine.commit(r.lease, success=False)
    remaining = engine._store.remaining_quota("quota:q:c1")
    print(f"\n[input] request failed, charge_on_failure=True")
    print(f"[verdict] remaining={remaining} (expect 1, consumed even on failure)")
    assert remaining == 1


def test_degrade_rolls_back_quota(engine):
    engine._registry.add_quota_rule(QuotaRule(id="q", quota=2))
    r = engine.decide("c1", "r", now=0.0)
    engine.rollback(r.lease)
    remaining = engine._store.remaining_quota("quota:q:c1")
    print(f"\n[input] degraded path, rollback")
    print(f"[verdict] remaining={remaining} (expect 2)")
    assert remaining == 2


def test_settlement_is_idempotent(engine):
    engine._registry.add_quota_rule(QuotaRule(id="q", quota=1))
    r = engine.decide("c1", "r", now=0.0)
    engine.commit(r.lease, True)
    engine.commit(r.lease, True)
    engine.rollback(r.lease)
    remaining = engine._store.remaining_quota("quota:q:c1")
    print(f"\n[input] commit twice then rollback once")
    print(f"[verdict] remaining={remaining} (expect 0, no double settlement)")
    assert remaining == 0


def test_verdicts_are_explainable(engine):
    engine._registry.add_rate_rule(RateRule(id="rl", limit=1, time_window_seconds=10))
    engine.decide("c1", "r", now=0.0)
    denied = engine.decide("c1", "r", now=0.0)
    text = denied.explain()
    print(f"\n[input] explain() output:\n{text}")
    assert "caller='c1'" in text
    assert "rule=rl" in text
    assert "rate limit 1 reached" in text
