# fastapi-gateway-throttling

基于 FastAPI 的网关限流与配额控制示例：按调用方维度做速率限制与配额扣减，
支持规则热更新、确定性多规则裁决，以及失败/降级路径的一致结算。

- 模型与配置、裁决规则详解：[`docs/throttling.md`](docs/throttling.md)
- 规则示例：[`rules.example.json`](rules.example.json)

## 快速开始

```bash
uv sync
THROTTLE_RULES_FILE=rules.example.json uv run python main.py
# 或不设环境变量，启动后用 PUT /api/rules 动态下发规则
```

## 测试

```bash
uv run pytest -s
```

测试覆盖并发超限、边界额度、429/402 原因区分、热更新与异常/降级结算，
日志中打印关键输入与每条规则的判定依据。
