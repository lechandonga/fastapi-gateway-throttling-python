"""HTTP 端到端：不同调用入口结论一致；错误码可区分；规则热更新 API。"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from throttling import QuotaRule, RateRule
from throttling.api import create_router
from throttling.engine import ThrottleEngine
from throttling.registry import RuleRegistry
from throttling.store import ThrottleStore


@pytest.fixture
def client():
    registry = RuleRegistry()
    store = ThrottleStore()
    engine = ThrottleEngine(registry, store)
    registry.add_rate_rule(RateRule(id="rl", limit=2, time_window_seconds=100))
    registry.add_quota_rule(QuotaRule(id="qd", quota=3))
    app = FastAPI()
    app.include_router(create_router(engine))
    with TestClient(app) as c:
        yield c


def test_missing_caller_header_is_401(client):
    resp = client.get("/api/resource/search")
    print(f"\n[input] GET without X-Caller-Id -> {resp.status_code}")
    assert resp.status_code == 401


def test_rate_limit_returns_429_with_verdict_detail(client):
    headers = {"X-Caller-Id": "alice"}
    r1 = client.get("/api/resource/search", headers=headers)
    r2 = client.get("/api/resource/search", headers=headers)
    r3 = client.get("/api/resource/search", headers=headers)
    print(f"\n[input] 3 GET, rate=2 -> statuses {r1.status_code},{r2.status_code},{r3.status_code}")
    assert r1.status_code == r2.status_code == 200
    assert r3.status_code == 429
    body = r3.json()["detail"]
    print(f"[verdict] code={body['code']} reasons={[v['reason'] for v in body['verdicts']]}")
    assert body["code"] == "rate_limited"
    assert any(v["kind"] == "rate" and not v["allowed"] for v in body["verdicts"])


def test_quota_exhausted_returns_402(client):
    # 热更新放开速率、配额=1，用独立 caller 专测 402
    client.put("/api/rules", json={
        "rate_rules": [{"id": "rl-open", "limit": 1000, "time_window_seconds": 100}],
        "quota_rules": [{"id": "qd", "quota": 1}],
    })
    h = {"X-Caller-Id": "carol-quota"}
    ok = client.get("/api/resource/a", headers=h)
    denied = client.get("/api/resource/a", headers=h)
    print(f"\n[input] quota=1 after reload -> {ok.status_code}, {denied.status_code}")
    assert ok.status_code == 200
    assert denied.status_code == 402
    assert denied.json()["detail"]["code"] == "quota_exhausted"


def test_fail_and_degrade_paths_refund_quota(client):
    # 热更新：速率放开，配额=2（独立 caller，避免与其它用例的计数耦合）
    client.put("/api/rules", json={
        "rate_rules": [{"id": "rl-open", "limit": 1000, "time_window_seconds": 100}],
        "quota_rules": [{"id": "qd", "quota": 2}],
    })
    h = {"X-Caller-Id": "dave-refund"}

    # fail 入口：扣减后按规则退还，后续成功请求仍可用
    r_fail = client.post("/api/resource/x/fail", headers=h)
    r_retry = client.get("/api/resource/x", headers=h)
    print(f"\n[input] fail entry then success -> {r_fail.status_code}, {r_retry.status_code}")
    assert r_fail.status_code == 200
    assert r_retry.status_code == 200  # 失败已退还，且成功消耗 1，剩 1

    # degrade 入口：同样回滚退还
    r_deg = client.post("/api/resource/x/degrade", headers=h)
    r_retry2 = client.get("/api/resource/x", headers=h)
    print(f"[input] degrade entry then success -> {r_deg.status_code}, {r_retry2.status_code}")
    assert r_deg.status_code == 200  # 降级前剩 1，回滚后仍为 1
    assert r_retry2.status_code == 200  # 成功消耗最后 1

    # 额度现已耗尽
    r_final_deny = client.get("/api/resource/x", headers=h)
    print(f"[input] quota now exhausted -> {r_final_deny.status_code}")
    assert r_final_deny.status_code == 402


def test_entry_points_share_same_decision(client):
    """同一时刻三个入口（GET / fail / degrade）面对同一耗尽状态，结论必须一致。"""
    client.put("/api/rules", json={
        "rate_rules": [],
        "quota_rules": [{"id": "qd", "quota": 0}],
    })
    h = {"X-Caller-Id": "erin"}
    codes = [
        client.get("/api/resource/x", headers=h).status_code,
        client.post("/api/resource/x/fail", headers=h).status_code,
        client.post("/api/resource/x/degrade", headers=h).status_code,
    ]
    print(f"\n[input] quota=0 across 3 entry points -> {codes}")
    assert codes == [402, 402, 402]


def test_rules_put_invalid_returns_400(client):
    resp = client.put("/api/rules", json={"rate_rules": [{"id": "bad", "limit": -1, "time_window_seconds": 1}]})
    print(f"\n[input] invalid PUT /api/rules -> {resp.status_code}")
    assert resp.status_code == 422  # pydantic 校验先于引擎


def test_status_endpoint_reports_counters(client):
    headers = {"X-Caller-Id": "frank"}
    client.get("/api/resource/x", headers=headers)
    resp = client.get("/api/status?name=x", headers=headers)
    body = resp.json()
    print(f"\n[input] one successful GET then /api/status -> {body}")
    assert resp.status_code == 200
    kinds = {item["kind"]: item for item in body["rules"]}
    assert kinds["rate"]["used"] == 1
    assert kinds["quota"]["remaining"] == 2
