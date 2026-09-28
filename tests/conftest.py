"""pytest 公共配置：打开 throttling 日志，打印关键输入与判定依据。"""

import logging
import sys

import pytest

from throttling import RuleRegistry, ThrottleEngine, ThrottleStore


@pytest.fixture
def engine():
    registry = RuleRegistry()
    store = ThrottleStore()
    return ThrottleEngine(registry, store)


def pytest_configure(config):
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(name)s | %(message)s"))
    logger = logging.getLogger("throttling")
    logger.setLevel(logging.INFO)
    logger.addHandler(handler)
