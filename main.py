"""FastAPI 入口：受限业务接口 + 规则管理接口，所有入口共用同一判定服务。"""
from __future__ import annotations

import asyncio

import uvicorn
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from app.throttling.limiter import TokenBucketLimiter
from app.throttling.models import DecisionReason, RateLimitRule
from app.throttling.quota import QuotaManager
from app.throttling.rules import RuleRegistry
from app.throttling.service import ThrottleService

registry = RuleRegistry()
limiter = TokenBucketLimiter()
quota = QuotaManager()
throttle = ThrottleService(registry, limiter, quota)

app = FastAPI(title="gateway-throttling")


class RuleIn(BaseModel):
    rule_id: str
    caller_pattern: str
    rate_per_second: float
    burst: int
    quota_limit: int
    priority: int = 0


async def _run_business_logic(caller: str, fail: bool) -> dict:
    """模拟下游业务；失败时走 report_failure 回滚配额。"""
    await asyncio.sleep(0)
    if fail:
        raise RuntimeError("downstream failure")
    return {"caller": caller, "result": "ok"}


async def _guarded_call(caller: str, fail: bool = False) -> dict:
    """统一判定入口：HTTP 与内部调用都走这里，保证结论一致。"""
    decision = await throttle.check(caller)
    if not decision.allowed:
        status = 429 if decision.reason is DecisionReason.RATE_LIMITED else 403
        raise HTTPException(
            status_code=status,
            detail={"reason": decision.reason.value, "rule_id": decision.rule_id,
                    "detail": decision.detail},
        )
    if decision.reason is DecisionReason.DEGRADED:
        return {"caller": caller, "result": "degraded-pass"}
    try:
        result = await _run_business_logic(caller, fail)
    except Exception:
        await throttle.report_failure(caller)  # 约定：失败回滚已预留配额
        raise HTTPException(status_code=502, detail="downstream failure, quota refunded")
    await throttle.report_success(caller)
    return result


@app.get("/")
def read_root():
    return {"Hello": "World"}


@app.get("/api/call")
async def call(caller: str, fail: bool = False):
    return await _guarded_call(caller, fail)


@app.post("/admin/rules", status_code=201)
def upsert_rule(rule: RuleIn):
    """动态新增/更新规则，立即生效，无需重启。"""
    registry.upsert(RateLimitRule(**rule.model_dump()))
    return {"ok": True}


@app.delete("/admin/rules/{rule_id}")
def delete_rule(rule_id: str):
    if not registry.remove(rule_id):
        raise HTTPException(status_code=404, detail="rule not found")
    return {"ok": True}


@app.get("/admin/rules")
def list_rules():
    return [r.__dict__ for r in registry.list_rules()]


@app.post("/admin/degrade")
def set_degrade(enabled: bool):
    throttle.set_degraded(enabled)
    return {"degraded": enabled}


@app.get("/admin/usage/{caller}")
def get_usage(caller: str):
    used, limit = quota.usage(caller)
    return {"caller": caller, "used": used, "limit": limit}


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=0)
