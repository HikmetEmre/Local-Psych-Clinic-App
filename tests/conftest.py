from __future__ import annotations

import pytest


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption("--e2e-users", default=None, help="JSON file with dev users for browser tests")
