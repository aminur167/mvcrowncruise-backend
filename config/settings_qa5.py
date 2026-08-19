"""Temporary QA settings: isolate this pass's test DB from other concurrent
QA sessions sharing the same Postgres server. Delete when done."""

from .settings import *  # noqa: F401,F403
from .settings import DATABASES

DATABASES["default"].setdefault("TEST", {})
DATABASES["default"]["TEST"]["NAME"] = "test_mv_alaska_qa5fix"
