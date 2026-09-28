"""限流与配额的核心数据模型（骨架：仅类型定义）。"""
from __future__ import annotations

import enum
from dataclasses import dataclass, field


class DecisionReason(enum.Enum):
    """判定原因，用于明确区分超限与配额耗尽。"""
    ALLOWED = "allowed"
    RATE_LIMITED = "rate_limited"      # 速率超限
    QUOTA_EXHAUSTED = "quota_exhausted"  # 配额耗尽
    DEGRADED = "degraded"              # 走降级路径放行


@dataclass(frozen=True)
class RateLimitRule:
    """一条限流规则。

    冲突解决是确定性的：priority 大者优先；并列时 caller_pattern 更具体
    （非通配）者优先；再并列按 rule_id 字典序取小者。
    """
    rule_id: str
    caller_pattern: str          # 精确 caller 名，或 "*" 通配
    rate_per_second: float       # 令牌桶速率
    burst: int                   # 桶容量
    quota_limit: int             # 周期内配额上限（0 表示不限制）
    priority: int = 0


@dataclass
class Decision:
    """一次请求的判定结果。"""
    allowed: bool
    reason: DecisionReason
    rule_id: str | None = None
    detail: str = ""


@dataclass
class Reservation:
    """配额预留句柄：成功路径 commit，失败/降级路径 refund。"""
    caller: str
    amount: int
    reservation_id: str
    committed: bool = field(default=False)
