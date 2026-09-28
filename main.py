"""FastAPI 网关入口：装配限流引擎并挂载路由。

规则可通过环境变量 THROTTLE_RULES_FILE 指定 JSON 文件，
或直接调用 PUT /api/rules 热更新（无需重启）。
"""

import logging
import os

import uvicorn
from fastapi import FastAPI

from throttling import RuleRegistry, ThrottleEngine, ThrottleStore
from throttling.api import create_router
from throttling.config import apply_config_file

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

registry = RuleRegistry()
store = ThrottleStore()
engine = ThrottleEngine(registry, store)

_rules_file = os.environ.get("THROTTLE_RULES_FILE")
if _rules_file:
    apply_config_file(registry, _rules_file)

app = FastAPI(title="FastAPI Gateway Throttling")
app.include_router(create_router(engine))


@app.get("/")
def read_root():
    return {"service": "fastapi-gateway-throttling", "rules_version": registry.version}


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8000)
