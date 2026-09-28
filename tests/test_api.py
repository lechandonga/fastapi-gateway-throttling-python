"""HTTP 入口测试：验证 API 与内部调用走同一判定服务，结论一致。"""
import asyncio
import logging

import httpx
import pytest

import main
from app.throttling.models import RateLimitRule

log = logging.getLogger("test.api")


@pytest.fixture
async def client():
    transport = httpx.ASGITransport(app=main.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        yield c
    # 每个用例后清理规则与降级开关，避免串扰
    for r in main.registry.list_rules():
        main.registry.remove(r.rule_id)
    main.throttle.set_degraded(False)


async def test_api_rate_limit_429(client):
    await client.post("/admin/rules", json={
        "rule_id": "r1", "caller_pattern": "api-user",
        "rate_per_second": 0.001, "burst": 2, "quota_limit": 0, "priority": 0})
    codes = []
    async def hit():
        return (await client.get("/api/call", params={"caller": "api-user"})).status_code
    codes = await asyncio.gather(*[hit() for _ in range(20)])
    log.info("basis: burst=2 concurrent=20 -> codes=%s", sorted(codes))
    assert sorted(codes) == [200, 200] + [429] * 18


async def test_api_quota_exhausted_403_and_reason_distinct(client):
    await client.post("/admin/rules", json={
        "rule_id": "r1", "caller_pattern": "q-user",
        "rate_per_second": 1e6, "burst": 100, "quota_limit": 2, "priority": 0})
    resps = [await client.get("/api/call", params={"caller": "q-user"})
             for _ in range(3)]
    last = resps[2]
    log.info("basis: quota=2, 3rd resp status=%s body=%s",
             last.status_code, last.json())
    assert [r.status_code for r in resps] == [200, 200, 403]
    assert last.json()["detail"]["reason"] == "quota_exhausted"


async def test_api_failure_refunds_quota(client):
    await client.post("/admin/rules", json={
        "rule_id": "r1", "caller_pattern": "f-user",
        "rate_per_second": 1e6, "burst": 100, "quota_limit": 2, "priority": 0})
    ok = await client.get("/api/call", params={"caller": "f-user"})
    failed = await client.get("/api/call", params={"caller": "f-user", "fail": True})
    after = await client.get("/api/call", params={"caller": "f-user"})
    log.info("basis: ok=%s failed=%s after_refund=%s", ok.status_code,
             failed.status_code, after.status_code)
    assert (ok.status_code, failed.status_code) == (200, 502)
    # 失败请求的预留已按约定回滚，配额腾出，后续请求放行
    assert after.status_code == 200
    usage = (await client.get("/admin/usage/f-user")).json()
    log.info("basis: usage after refund=%s", usage)
    assert usage["used"] == 2  # 两笔成功提交，失败那笔已退回又被自己消耗
