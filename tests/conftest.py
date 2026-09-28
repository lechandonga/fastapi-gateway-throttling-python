import logging

import pytest

from app.throttling.limiter import TokenBucketLimiter
from app.throttling.quota import QuotaManager
from app.throttling.rules import RuleRegistry
from app.throttling.service import ThrottleService

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")


@pytest.fixture
def registry():
    return RuleRegistry()


@pytest.fixture
def limiter():
    return TokenBucketLimiter()


@pytest.fixture
def quota():
    return QuotaManager()


@pytest.fixture
def service(registry, limiter, quota):
    return ThrottleService(registry, limiter, quota)
