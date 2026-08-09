"""conftest.py for spgloader tests — shared fixtures and CLI options."""
from pathlib import Path
import pytest


def pytest_addoption(parser):
    parser.addoption("--workspace", action="store", default=None,
                     help="Path to a real spgloader workspace for live contract validation")


@pytest.fixture
def workspace(request):
    ws = request.config.getoption("--workspace")
    if ws:
        return Path(ws)
    return None
