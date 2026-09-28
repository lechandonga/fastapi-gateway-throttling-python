# fastapi-gateway-throttling-python

FastAPI 网关的请求限流与配额控制示例：按调用方（caller）维度做速率限制与配额扣减，
并发下计数精确，超限与配额耗尽明确区分，规则支持运行时动态调整。

## 限流模型

```
请求 -> ThrottleService.check(caller)
          1. 降级模式？        -> 放行(DEGRADED)，不记账
          2. 无命中规则？      -> 放行(ALLOWED)，默认开放
          3. 令牌桶速率检查    -> 超限拒绝 RATE_LIMITED (HTTP 429)
          4. 配额预留 reserve  -> 耗尽拒绝 QUOTA_EXHAUSTED (HTTP 403)
          5. 放行              -> 配额处于"已预留"状态
业务成功 -> report_success -> 预留转为正式消耗
业务失败 -> report_failure -> 预留按约定回滚（仅预留中的请求可退，防重复退额）
```

- **速率限制**：每 caller 一个令牌桶（`rate_per_second` + `burst`），
  所有读改写在同一把 `asyncio.Lock` 内，并发下不漏算、不重复扣算。
- **配额**：两阶段 reserve -> commit / refund。reserve 在锁内检查并记账，
  并发下不会超卖；失败/降级路径按约定回滚。
- **判定原因可区分**：`Decision.reason` 为
  `allowed / rate_limited / quota_exhausted / degraded` 四态，
  HTTP 层分别映射 200 / 429 / 403 / 200(degraded-pass)。
- **一致性**：所有调用入口（HTTP `/api/call`、内部 `_guarded_call`）
  共用同一个 `ThrottleService` 实例，判定结论一致。

## 规则配置

规则通过管理接口在运行时增删改，**立即生效，无需重启**：

```bash
# 新增/更新规则
curl -X POST localhost:8000/admin/rules -H 'content-type: application/json' -d '{
  "rule_id": "r1", "caller_pattern": "alice",
  "rate_per_second": 10, "burst": 20, "quota_limit": 1000, "priority": 1
}'
curl localhost:8000/admin/rules            # 查看全部规则
curl -X DELETE localhost:8000/admin/rules/r1   # 删除
curl -X POST 'localhost:8000/admin/degrade?enabled=true'  # 开关降级
curl localhost:8000/admin/usage/alice      # 查看配额用量
```

字段说明：

| 字段 | 含义 |
|---|---|
| `caller_pattern` | 精确 caller 名，或 `"*"` 通配所有调用方 |
| `rate_per_second` / `burst` | 令牌桶速率与容量 |
| `quota_limit` | 配额上限，`0` 表示不限制 |
| `priority` | 冲突裁决优先级，数值大者优先 |

**多规则同时命中的确定性裁决**（不随机）：`priority` 大者优先 →
并列时精确匹配优先于 `"*"` 通配 → 再并列取 `rule_id` 字典序小者。
规则不变时同一 caller 永远命中同一条规则，判定可解释、可复现。

## 验证方法

```bash
uv run pytest tests/ -q -s
```

测试覆盖（`-s` 可在日志中看到关键输入与判定依据，形如 `input: ...` / `basis: ...`）：

- `test_concurrent_rate_limit_no_overcount`：200 并发打 burst=10，恰好放行 10 个；
- `test_concurrent_quota_no_oversell`：100 并发抢 quota=7，恰好 7 个成功，用量精确；
- `test_boundary_quota_exact`：第 N 个放行、第 N+1 个拒绝，边界原因正确；
- `test_reason_distinction_rate_vs_quota`：超限与配额耗尽原因明确区分；
- `test_rule_conflict_deterministic`：多规则命中裁决确定且可重复；
- `test_dynamic_rule_update_takes_effect`：运行中改规则立即生效；
- `test_failure_refund_and_no_double_refund`：失败回滚、不重复退额；
- `test_degraded_mode_passthrough`：降级放行不记账；
- `tests/test_api.py`：HTTP 入口的 429/403/502 语义与失败回滚端到端验证。

## 运行

```bash
uv run python main.py   # 或 uv run uvicorn main:app
```
