"""FastAPI 路由：受保护资源入口、规则热更新、状态查询。

所有入口共用同一个 ThrottleEngine，保证不同入口的判定结论一致：
- GET  /api/resource/{name}  正常入口：处理成功 -> commit(success=True)
- POST /api/resource/{name}/fail   模拟下游失败：commit(success=False)，按规则退额度
- POST /api/resource/{name}/degrade 模拟降级：rollback()，按约定退还/保留
- GET  /api/status             查询某调用方/资源的计数视图
- PUT  /api/rules              原子热更新整套规则
- GET  /api/rules              查看当前规则快照
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, Field

from .config import apply_config
from .engine import ThrottleEngine
from .models import Decision

logger = logging.getLogger("throttling.api")


class RateRuleModel(BaseModel):
    id: str
    limit: int = Field(gt=0)
    time_window_seconds: float = Field(gt=0)
    scope: str = "caller"
    resource: str | None = None
    priority: int = 0
    description: str = ""


class QuotaRuleModel(BaseModel):
    id: str
    quota: int = Field(ge=0)
    scope: str = "caller"
    resource: str | None = None
    priority: int = 0
    charge_on_failure: bool = False
    description: str = ""


class RulesPayload(BaseModel):
    rate_rules: list[RateRuleModel] = []
    quota_rules: list[QuotaRuleModel] = []


def require_caller(x_caller_id: str | None = Header(default=None)) -> str:
    if not x_caller_id:
        raise HTTPException(status_code=401, detail="missing X-Caller-Id header")
    return x_caller_id


def _deny_http(status_code: int, code: str, message: str, result) -> HTTPException:
    detail = {
        "code": code,
        "message": message,
        "caller_id": result.caller_id,
        "resource": result.resource,
        "verdicts": [
            {
                "rule_id": v.rule_id,
                "kind": v.kind,
                "used": v.used,
                "limit": v.limit,
                "allowed": v.allowed,
                "reason": v.reason,
            }
            for v in result.verdicts
        ],
    }
    logger.info("request denied: %s | %s", code, result.explain().replace("\n", " | "))
    return HTTPException(status_code=status_code, detail=detail)


def create_router(engine: ThrottleEngine) -> APIRouter:
    router = APIRouter(prefix="/api")

    def _admit(caller_id: str, resource: str):
        result = engine.decide(caller_id, resource)
        if result.decision is Decision.QUOTA_EXHAUSTED:
            raise _deny_http(402, "quota_exhausted", "quota exhausted", result)
        if result.decision is Decision.RATE_LIMITED:
            raise _deny_http(429, "rate_limited", "rate limit exceeded", result)
        return result

    @router.get("/resource/{name}")
    def get_resource(name: str, caller_id: str = Depends(require_caller)):
        result = _admit(caller_id, name)
        # 正常业务路径
        engine.commit(result.lease, success=True)
        return {
            "status": "ok",
            "caller_id": caller_id,
            "resource": name,
            "verdicts": [v.reason for v in result.verdicts],
        }

    @router.post("/resource/{name}/fail")
    def fail_resource(name: str, caller_id: str = Depends(require_caller)):
        result = _admit(caller_id, name)
        # 下游处理失败：按规则的 charge_on_failure 决定退还是留
        engine.commit(result.lease, success=False)
        return {
            "status": "failed",
            "caller_id": caller_id,
            "resource": name,
            "note": "quota refunded unless charge_on_failure=true; rate slot retained",
        }

    @router.post("/resource/{name}/degrade")
    def degrade_resource(name: str, caller_id: str = Depends(require_caller)):
        result = _admit(caller_id, name)
        # 降级路径：按约定回滚配额（速率名额保留，因为请求确实到达）
        engine.rollback(result.lease)
        return {
            "status": "degraded",
            "caller_id": caller_id,
            "resource": name,
            "note": "quota rolled back; rate slot retained",
        }

    @router.get("/status")
    def get_status(name: str, caller_id: str = Depends(require_caller)):
        return {"caller_id": caller_id, "resource": name, "rules": engine.status(caller_id, name)}

    @router.put("/rules")
    def update_rules(payload: RulesPayload):
        try:
            apply_config(engine._registry, payload.model_dump())
        except ValueError as exc:
            raise HTTPException(status_code=400, detail={"code": "invalid_rules", "message": str(exc)})
        rates, quotas = engine._registry.snapshot()
        return {
            "status": "applied",
            "version": engine._registry.version,
            "rate_rules": len(rates),
            "quota_rules": len(quotas),
        }

    @router.get("/rules")
    def get_rules():
        rates, quotas = engine._registry.snapshot()
        return {
            "version": engine._registry.version,
            "rate_rules": [r.__dict__ for r in rates],
            "quota_rules": [q.__dict__ for q in quotas],
        }

    return router
