"""领域模型：规则、判定结论、租约等类型定义。"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class LimitScope(str, Enum):
    """限流维度。"""

    CALLER = "caller"
    CALLER_RESOURCE = "caller_resource"
    GLOBAL = "global"


class Decision(str, Enum):
    """最终判定。"""

    ALLOW = "allow"
    RATE_LIMITED = "rate_limited"
    QUOTA_EXHAUSTED = "quota_exhausted"


@dataclass(frozen=True)
class RateRule:
    """速率规则：在 time_window_seconds 秒内最多允许 limit 次。"""

    id: str
    limit: int
    time_window_seconds: float
    scope: LimitScope = LimitScope.CALLER
    resource: str | None = None
    priority: int = 0
    description: str = ""


@dataclass(frozen=True)
class QuotaRule:
    """配额规则：周期内总额度 quota，失败请求是否退还由 charge_on_failure 决定。"""

    id: str
    quota: int
    scope: LimitScope = LimitScope.CALLER
    resource: str | None = None
    priority: int = 0
    charge_on_failure: bool = False
    description: str = ""


@dataclass(frozen=True)
class RuleVerdict:
    """单条规则的判定依据。"""

    rule_id: str
    kind: str
    allowed: bool
    reason: str
    used: int = 0
    limit: int = 0


@dataclass
class Lease:
    """一次放行后持有的额度租约，用于结算（提交/回滚）。"""

    caller_id: str
    resource: str
    rate_rule_id: str | None
    quota_rule_id: str | None
    rate_scope: LimitScope | None = None
    quota_scope: LimitScope | None = None
    charge_on_failure: bool = False
    committed: bool = False
    rolled_back: bool = False


@dataclass(frozen=True)
class DecisionResult:
    """准入判定结论。"""

    decision: Decision
    caller_id: str
    resource: str
    verdicts: tuple[RuleVerdict, ...] = field(default_factory=tuple)
    lease: Lease | None = None

    @property
    def allowed(self) -> bool:
        return self.decision is Decision.ALLOW

    def explain(self) -> str:
        """生成可解释的判定文本。"""
        lines = [
            f"caller={self.caller_id!r} resource={self.resource!r} "
            f"decision={self.decision.value}"
        ]
        for v in self.verdicts:
            lines.append(
                f"  - rule={v.rule_id} kind={v.kind} used={v.used}/{v.limit} "
                f"allowed={v.allowed} reason={v.reason}"
            )
        return "\n".join(lines)


@dataclass(frozen=True)
class QuotaStatus:
    """某调用方在某资源上的剩余额度视图。"""

    caller_id: str
    resource: str
    quota: int
    remaining: int
