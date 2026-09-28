# 限流与配额模型说明

本文档说明网关的请求限流（rate limit）与配额控制（quota）模型、规则配置方式、
动态调整与冲突裁决规则、异常/降级时的额度结算约定，以及验证方法。

## 1. 模型概览

系统由四个部分组成（均在 `throttling/` 包内）：

| 模块 | 职责 |
| --- | --- |
| `models.py` | 领域类型：`RateRule` / `QuotaRule` / `DecisionResult` / `RuleVerdict` / `Lease` |
| `store.py` | 并发安全的内存计数：滑动窗口速率计数 + 配额余额，单锁保证读改写原子性 |
| `registry.py` | 规则注册表：增删改、原子快照、版本号，支持热更新 |
| `engine.py` | 判定引擎：规则匹配、确定性裁决、原子扣减、提交/回滚结算 |
| `config.py` | JSON 规则装载与字段校验 |
| `api.py` | FastAPI 路由：受保护入口、规则热更新、状态查询 |

### 两种限制的区别（明确区分，不混用）

| 维度 | 速率限制 RateRule | 配额 QuotaRule |
| --- | --- | --- |
| 含义 | 滑动时间窗口内的请求频率上限 | 计费周期内的累计总量上限 |
| 恢复方式 | 窗口滑动后自动恢复 | 周期内不恢复，需重置/改规则 |
| 耗尽时结论 | `rate_limited`（HTTP 429） | `quota_exhausted`（HTTP 402） |
| 失败/降级 | 名额保留（请求确实到达） | 按约定退还或保留 |

判定结论三态：`allow` / `rate_limited` / `quota_exhausted`。
响应体（拒绝时）携带每条命中规则的 `used / limit / reason`，结论可解释。

### 请求生命周期与两阶段结算

```
调用方请求
   │
   ▼
engine.decide(caller, resource)
   ├─ 取规则快照（一致性版本）
   ├─ 匹配 + 确定性排序所有命中规则
   ├─ 锁内原子扣减：速率名额 + 配额额度（任一失败则整体补偿回滚）
   └─ 返回 allow + Lease，或明确的拒绝原因
   │（allow 之后执行业务）
   ▼
业务成功 ──► engine.commit(lease, success=True)   # 配额保留扣减
业务失败 ──► engine.commit(lease, success=False)  # 按 charge_on_failure 决定
走降级   ──► engine.rollback(lease)               # 约定：退还配额，速率名额保留
```

`Lease` 冻结了扣减时刻生效规则的 `scope` 与 `charge_on_failure`，
因此**结算时即使规则已被热更新/删除，仍按请求准入时的约定执行**。
重复 `commit/rollback` 是幂等的，不会重复退还。

## 2. 规则配置方式

规则为 JSON 格式，根对象包含 `rate_rules` 与 `quota_rules` 两个数组，
完整示例见仓库根目录 `rules.example.json`。

### 速率规则字段

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `id` | string | 是 | 规则唯一标识，计数键也以它隔离 |
| `limit` | int > 0 | 是 | 窗口内最大请求数 |
| `time_window_seconds` | float > 0 | 是 | 窗口长度（秒） |
| `scope` | string | 否 | `caller`（默认）/ `caller_resource` / `global` |
| `resource` | string | 否 | `caller_resource`/`global` 下指定资源；`"*"` 或缺省表示全部 |
| `priority` | int | 否 | 同一具体度下的优先级，大者优先，默认 0 |
| `description` | string | 否 | 备注 |

### 配额规则字段

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `id` | string | 是 | 规则唯一标识 |
| `quota` | int ≥ 0 | 是 | 周期总额度；`0` 表示完全封禁 |
| `scope` / `resource` / `priority` | - | 否 | 同速率规则 |
| `charge_on_failure` | bool | 否 | `false`（默认）：业务失败退还额度；`true`：失败也计费 |

装载方式：

1. 启动加载：设置环境变量 `THROTTLE_RULES_FILE=rules.example.json` 后运行 `python main.py`；
2. 动态热更新：`PUT /api/rules`（见下节），无需重启。

非法配置（limit≤0、窗口≤0、未知 scope、quota<0、id 重复）会被拒绝并返回 4xx，
线上正在使用的旧规则不受影响（整体替换是原子的）。

## 3. 动态调整与多规则命中的确定性裁决

### 原子热更新

- 所有规则更新在注册表的锁内完成，版本号自增；
- 引擎每次判定只取一份不可变快照，绝不会读到「半更新」状态；
- 检测到版本变化时，引擎清理已删除规则的计数键，避免泄漏；
- 配额额度动态调整的语义是**保留累计消耗量**：
  `remaining = max(0, new_quota - consumed)`。
  连续多次调大调小结果都确定（消耗量独立累计，不依赖上一版额度反推）。

### 多条规则同时命中如何裁决

所有命中的规则**各自独立计数并全部参与判定**；最终是否放行要求每条命中规则都满足。
规则的「生效顺序」（用于解释与展示）严格按以下排序键，结论唯一、可复现：

1. **具体度**（从高到低）：`caller_resource`（精确 resource）> `caller_resource`（通配）
   > `caller` > `global`（精确 resource）> `global`（通配）。
   具体度按排序键 `(scope 排名, 是否精确资源)` 比较，即先比 scope，再比资源是否精确；
2. **priority**：数值大者优先；
3. **规则 id 字典序**：小者优先（最终兜底，保证同一输入永远得到同一结论，绝不随机）。

同时命中「速率超限」和「配额耗尽」时，**统一报告 `quota_exhausted`**：
配额是硬上限且不会自动恢复，速率名额随窗口恢复，先报告更严格、更持久的限制。
响应中两条规则的判定依据都会给出，可解释性不受影响。

## 4. 并发正确性

- 存储层所有「读—判断—写」操作在同一把锁内完成；引擎层另用 RLock
  串行化一次完整的「选规则 + 速率扣减 + 配额扣减」，并与结算互斥。
- 准入扣减是原子的：若配额扣减失败，此前本次请求已扣的速率名额会被精确补偿移除，
  已扣的其它配额会退还——不会出现部分扣减导致的漏算/多算。
- 速率计数使用「时间戳双端队列 + 半开区间 `(now-window, now]`」，
  窗口边界行为确定：恰好在 `now - window` 时刻的请求在 `now` 时已滑出。

## 5. HTTP 接口

所有业务接口需携带请求头 `X-Caller-Id`，缺失返回 401。

| 方法/路径 | 说明 |
| --- | --- |
| `GET /api/resource/{name}` | 正常入口；成功后提交，200 |
| `POST /api/resource/{name}/fail` | 模拟下游失败；按 `charge_on_failure` 结算 |
| `POST /api/resource/{name}/degrade` | 模拟降级；回滚配额、保留速率名额 |
| `GET /api/status?name={resource}` | 查询命中规则与实时计数 |
| `PUT /api/rules` | 原子热更新整套规则 |
| `GET /api/rules` | 查看当前规则快照与版本号 |

拒绝响应示例（429）：

```json
{
  "detail": {
    "code": "rate_limited",
    "message": "rate limit exceeded",
    "caller_id": "alice",
    "resource": "search",
    "verdicts": [
      {"rule_id": "caller-search-burst", "kind": "rate", "used": 10, "limit": 10,
       "allowed": false, "reason": "rate limit 10 reached in 1.0s window"},
      {"rule_id": "daily-quota", "kind": "quota", "used": 23, "limit": 10000,
       "allowed": true, "reason": "9977/10000 quota remaining"}
    ]
  }
}
```

## 6. 验证方法

```bash
uv sync                       # 安装依赖（含 pytest/httpx）
uv run pytest -s              # 运行全部单测，-s 可看到判定依据日志
uv run python main.py         # 启动服务（默认 127.0.0.1:8000）
THROTTLE_RULES_FILE=rules.example.json uv run python main.py
```

测试覆盖（`tests/`）：

- `test_concurrency.py`：200 线程并发下速率放行数恰为上限、配额不超扣/不出现负余额；
- `test_boundaries.py`：窗口滑动边界、最后一个配额、零配额封禁、配额动态调整、调用方隔离；
- `test_reasons.py`：429 与 402 原因区分、同时命中时的优先级、失败/降级结算与幂等；
- `test_rules.py`：具体度优先于 priority、id 兜底确定性、热更新即时生效、非法配置拒绝；
- `test_api.py`：HTTP 状态码、三个入口结论一致、热更新 API、状态查询。

每个用例都通过 `print` 输出关键输入（并发数、限额、时间点、入口）与判定依据
（命中规则、used/limit、最终 decision），同时 `throttling` 日志也输出完整 explain 文本。

手工验证示例：

```bash
curl -s -H 'X-Caller-Id: alice' http://127.0.0.1:8000/api/resource/search
curl -s -X PUT http://127.0.0.1:8000/api/rules \
  -H 'Content-Type: application/json' \
  -d '{"rate_rules":[{"id":"rl","limit":1,"time_window_seconds":10}],"quota_rules":[]}'
curl -s -H 'X-Caller-Id: alice' 'http://127.0.0.1:8000/api/status?name=search'
```
