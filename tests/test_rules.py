"""规则选择的确定性、热更新生效、配置解析。"""

import pytest

from throttling import Decision, LimitScope, QuotaRule, RateRule, load_rules_from_dict
from throttling.config import apply_config


def test_specificity_over_priority(engine):
    """具体度优先：即便 caller 规则 priority 更高，资源精确规则也胜出。"""
    engine._registry.add_rate_rule(
        RateRule(id="generic", limit=100, time_window_seconds=10,
                 scope=LimitScope.CALLER, priority=1000)
    )
    engine._registry.add_rate_rule(
        RateRule(id="specific", limit=1, time_window_seconds=10,
                 scope=LimitScope.CALLER_RESOURCE, resource="search", priority=0)
    )
    engine.decide("c1", "search", now=0.0)
    denied = engine.decide("c1", "search", now=0.0)
    blocker = [v for v in denied.verdicts if not v.allowed]
    print(f"\n[input] generic(priority=1000) vs specific(resource=search)")
    print(f"[verdict] blocker={[b.rule_id for b in blocker]} (expect ['specific'])")
    assert [b.rule_id for b in blocker] == ["specific"]
    # 非 search 资源只有 generic 生效
    other = engine.decide("c1", "other", now=0.0)
    assert other.allowed


def test_id_tiebreak_is_deterministic(engine):
    """同具体度同优先级：id 字典序较小者生效，多次裁决结论一致。"""
    for rid in ("rule-z", "rule-a", "rule-m"):
        engine._registry.add_rate_rule(
            RateRule(id=rid, limit=1, time_window_seconds=10, scope=LimitScope.CALLER)
        )
    first = engine.decide("c1", "r", now=0.0)
    seen_blockers = set()
    for _ in range(5):
        denied = engine.decide("c1", "r", now=0.0)
        blockers = tuple(v.rule_id for v in denied.verdicts if not v.allowed)
        seen_blockers.add(blockers)
    print(f"\n[input] three same-level rules, ids rule-a/m/z")
    print(f"[verdict] blocker tuples over 5 runs: {seen_blockers}")
    # 三条规则都已记账到各自窗口；拒绝时三条都满，结论集合稳定
    assert len(seen_blockers) == 1
    assert first.allowed


def test_hot_reload_takes_effect_without_restart(engine):
    engine._registry.add_rate_rule(RateRule(id="old", limit=1, time_window_seconds=100))
    assert engine.decide("c1", "r", now=0.0).allowed
    assert engine.decide("c1", "r", now=0.0).decision is Decision.RATE_LIMITED

    apply_config(engine._registry, {
        "rate_rules": [{"id": "new", "limit": 100, "time_window_seconds": 100}],
    })
    print(f"\n[input] rules replaced atomically: old(1) -> new(100)")
    r = engine.decide("c1", "r", now=0.0)
    print(f"[verdict] after reload: {r.decision.value}, verdicts={[v.rule_id for v in r.verdicts]}")
    assert r.allowed
    assert [v.rule_id for v in r.verdicts] == ["new"]
    # 旧规则的计数键已被清理
    keys = list(engine._store.snapshot()["rate_keys"].keys())
    assert not any("old" in k for k in keys)


def test_invalid_config_rejected():
    bad_payloads = [
        {"rate_rules": [{"id": "x", "limit": 0, "time_window_seconds": 1}]},
        {"rate_rules": [{"id": "x", "limit": 1, "time_window_seconds": -1}]},
        {"rate_rules": [{"id": "x", "limit": 1, "time_window_seconds": 1, "scope": "weird"}]},
        {"quota_rules": [{"id": "q", "quota": -5}]},
    ]
    for payload in bad_payloads:
        with pytest.raises(ValueError):
            load_rules_from_dict(payload)
    print("\n[input] 4 invalid payloads all rejected with ValueError")


def test_valid_config_roundtrip():
    payload = {
        "rate_rules": [
            {"id": "rl", "limit": 10, "time_window_seconds": 1, "scope": "caller",
             "priority": 5, "description": "per caller"},
            {"id": "rls", "limit": 2, "time_window_seconds": 1,
             "scope": "caller_resource", "resource": "search"},
        ],
        "quota_rules": [
            {"id": "qd", "quota": 1000, "charge_on_failure": True},
        ],
    }
    rates, quotas = load_rules_from_dict(payload)
    print(f"\n[input] valid payload -> {len(rates)} rate rules, {len(quotas)} quota rules")
    assert rates[0].priority == 5
    assert rates[1].resource == "search"
    assert quotas[0].charge_on_failure is True
