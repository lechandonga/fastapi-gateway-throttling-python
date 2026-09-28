"""边界额度：窗口滑动、零配额、最后一个额度、动态调整边界。"""

from throttling import Decision, QuotaRule, RateRule
from throttling.models import LimitScope


def test_rate_window_sliding_boundary(engine):
    engine._registry.add_rate_rule(RateRule(id="rl", limit=2, time_window_seconds=10))

    r1 = engine.decide("c1", "r", now=0.0)
    r2 = engine.decide("c1", "r", now=9.9)
    # 半开区间 (t-window, t]：t=10.0 时 t=0.0 的记录恰好滑出，可放行
    r3 = engine.decide("c1", "r", now=10.0)
    print(f"\n[input] limit=2/10s, requests at t=0.0, 9.9, 10.0 (half-open window)")
    print(f"[verdict] {r1.decision.value}, {r2.decision.value}, {r3.decision.value}")
    assert r1.allowed and r2.allowed and r3.allowed

    # 同一时刻再来一次：窗口内是 [9.9, 10.0]，拒绝
    r4 = engine.decide("c1", "r", now=10.0)
    # t=10.1：0.0 已滑出（9.9 仍在），窗口内 [9.9,10.0] 已满两条 -> 仍拒绝
    r5 = engine.decide("c1", "r", now=19.9001)
    print(f"[input] repeat at t=10.0 -> {r4.decision.value}; at t=19.9001 -> {r5.decision.value}")
    assert r4.decision is Decision.RATE_LIMITED
    assert r5.allowed
    for lease in (r1.lease, r2.lease, r3.lease, r5.lease):
        engine.commit(lease, True)


def test_quota_last_unit_boundary(engine):
    engine._registry.add_quota_rule(QuotaRule(id="q", quota=1))
    r1 = engine.decide("c1", "r", now=0.0)
    r2 = engine.decide("c1", "r", now=0.0)
    print(f"\n[input] quota=1, two sequential requests")
    print(f"[verdict] first={r1.decision.value}, second={r2.decision.value}")
    assert r1.allowed
    assert r2.decision is Decision.QUOTA_EXHAUSTED
    engine.commit(r1.lease, True)

    # 回滚后最后一个额度可再次被使用
    engine2 = engine
    d = engine2.decide("c2", "r", now=0.0)
    engine2.rollback(d.lease)
    again = engine2.decide("c2", "r", now=0.0)
    print(f"[verdict] after rollback, retry={again.decision.value}")
    assert again.allowed
    engine2.commit(again.lease, True)


def test_zero_quota_rejects_all(engine):
    engine._registry.add_quota_rule(QuotaRule(id="q0", quota=0))
    r = engine.decide("c1", "r", now=0.0)
    print(f"\n[input] quota=0 -> {r.decision.value}")
    assert r.decision is Decision.QUOTA_EXHAUSTED
    assert r.lease is None


def test_quota_resize_preserves_consumption(engine):
    engine._registry.add_quota_rule(QuotaRule(id="q", quota=10))
    for i in range(4):
        d = engine.decide("c1", "r", now=float(i))
        assert d.allowed
        engine.commit(d.lease, True)
    print(f"\n[input] consumed 4/10, then resize quota 10 -> 6 (should leave 2)")
    engine._registry.add_quota_rule(QuotaRule(id="q", quota=6))
    status = {s["rule_id"]: s for s in engine.status("c1", "r")}
    print(f"[verdict] status={status['q']}")
    assert status["q"]["remaining"] == 2
    # 调小到已消耗量以下：剩余归零，不报错
    engine._registry.add_quota_rule(QuotaRule(id="q", quota=2))
    status = {s["rule_id"]: s for s in engine.status("c1", "r")}
    print(f"[verdict] resize to 2 -> {status['q']}")
    assert status["q"]["remaining"] == 0
    # 调大后立即可用
    engine._registry.add_quota_rule(QuotaRule(id="q", quota=100))
    status = {s["rule_id"]: s for s in engine.status("c1", "r")}
    print(f"[verdict] resize to 100 -> remaining={status['q']['remaining']}")
    assert status["q"]["remaining"] == 96


def test_caller_isolation(engine):
    engine._registry.add_rate_rule(RateRule(id="rl", limit=1, time_window_seconds=10))
    a1 = engine.decide("alice", "r", now=0.0)
    b1 = engine.decide("bob", "r", now=0.0)
    a2 = engine.decide("alice", "r", now=0.0)
    print(f"\n[input] limit=1 per caller: alice, bob, alice")
    print(f"[verdict] {a1.decision.value}, {b1.decision.value}, {a2.decision.value}")
    assert a1.allowed and b1.allowed
    assert a2.decision is Decision.RATE_LIMITED
    engine.commit(a1.lease, True)
    engine.commit(b1.lease, True)
