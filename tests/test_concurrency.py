"""并发场景：高并发下计数不能漏算或重复扣算。"""

import threading

from throttling import Decision, QuotaRule, RateRule


def test_concurrent_rate_limit_exact_boundary(engine):
    """200 并发、速率上限 50：放行数必须恰好 50，拒绝数 150。"""
    engine._registry.add_rate_rule(RateRule(id="rl", limit=50, time_window_seconds=60))

    decisions = []
    lock = threading.Lock()

    def worker(i):
        result = engine.decide(f"caller-{i % 5}", "search", now=100.0)
        with lock:
            decisions.append(result.decision)
        if result.lease is not None:
            engine.commit(result.lease, True)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(200)]
    print(f"\n[input] 200 threads, 5 callers x rate limit 50/60s, same timestamp=100")
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    allowed = sum(1 for d in decisions if d is Decision.ALLOW)
    limited = sum(1 for d in decisions if d is Decision.RATE_LIMITED)
    print(f"[verdict] allowed={allowed} rate_limited={limited}")
    assert allowed == 200  # 5 个调用方各自独立 50
    assert limited == 0

    # 单调用方 200 并发、上限 50
    decisions.clear()

    def single_caller(i):
        result = engine.decide("hot-caller", "search", now=200.0)
        with lock:
            decisions.append(result.decision)
        if result.lease is not None:
            engine.commit(result.lease, True)

    threads = [threading.Thread(target=single_caller, args=(i,)) for i in range(200)]
    print(f"[input] 200 threads, single caller, rate limit 50/60s")
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    allowed = sum(1 for d in decisions if d is Decision.ALLOW)
    limited = sum(1 for d in decisions if d is Decision.RATE_LIMITED)
    print(f"[verdict] allowed={allowed} rate_limited={limited}")
    assert allowed == 50
    assert limited == 150
    # 窗口内计数恰好等于上限，没有漏记/多记
    used = engine._store.peek_rate("rate:rl:hot-caller", 200.0, 60.0)
    print(f"[store] recorded hits in window={used}")
    assert used == 50


def test_concurrent_quota_no_over_deduct(engine):
    """200 并发、配额 37：成功扣减数必须恰好 37，且不会出现负余额。"""
    engine._registry.add_quota_rule(QuotaRule(id="qd", quota=37))

    outcomes = []
    lock = threading.Lock()

    def worker():
        result = engine.decide("c1", "pay", now=0.0)
        with lock:
            outcomes.append(result.decision)
        if result.lease is not None:
            engine.commit(result.lease, True)

    threads = [threading.Thread(target=worker) for _ in range(200)]
    print("\n[input] 200 threads, quota=37")
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    allowed = sum(1 for d in outcomes if d is Decision.ALLOW)
    exhausted = sum(1 for d in outcomes if d is Decision.QUOTA_EXHAUSTED)
    remaining = engine._store.remaining_quota("quota:qd:c1")
    print(f"[verdict] allowed={allowed} quota_exhausted={exhausted} remaining={remaining}")
    assert allowed == 37
    assert exhausted == 163
    assert remaining == 0
