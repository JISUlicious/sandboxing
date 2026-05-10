"""Pytest fixtures + CLI for the load harness.

Decoupled from the unit-test conftest at tests/conftest.py — the load
harness talks to a real running API (FastAPI live process), not a
TestClient. The unit conftest's `client`/`authed`/`service` fixtures
are intentionally not visible from this directory because pytest only
auto-collects conftests on the current path.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from .host_baseline import capture_host_baseline


def pytest_addoption(parser: pytest.Parser) -> None:
    g = parser.getgroup("load")
    g.addoption(
        "--base-url",
        default=None,
        help="Sandbox API base URL. Falls back to LOAD_BASE_URL env.",
    )
    g.addoption(
        "--token",
        default=None,
        help="Bearer token for the API. Falls back to LOAD_TOKEN env.",
    )
    g.addoption(
        "--max-sessions",
        type=int,
        default=None,
        help="Cap on the ramp ceiling. Default 100 (LOAD_MAX_SESSIONS env).",
    )
    g.addoption(
        "--duration-s",
        type=int,
        default=None,
        help="Per-level steady-state seconds. Default 60 (LOAD_DURATION_S env).",
    )
    g.addoption(
        "--results-dir",
        default=None,
        help=(
            "Directory for ramp_*.json + host_baseline_*.json. "
            "Default tests/load/results (LOAD_RESULTS_DIR env)."
        ),
    )


def _flag_or_env(config: pytest.Config, flag: str, env: str, default: str | None) -> str | None:
    return config.getoption(flag) or os.environ.get(env) or default


def _flag_or_env_int(config: pytest.Config, flag: str, env: str, default: int) -> int:
    v = config.getoption(flag)
    if v is not None:
        return int(v)
    raw = os.environ.get(env)
    return int(raw) if raw else default


@pytest.fixture(scope="session")
def base_url(pytestconfig: pytest.Config) -> str:
    url = _flag_or_env(pytestconfig, "--base-url", "LOAD_BASE_URL", None)
    if not url:
        pytest.skip("--base-url / LOAD_BASE_URL not set; load harness is opt-in.")
    return url.rstrip("/")


@pytest.fixture(scope="session")
def token(pytestconfig: pytest.Config) -> str:
    tok = _flag_or_env(pytestconfig, "--token", "LOAD_TOKEN", None)
    if not tok:
        pytest.skip("--token / LOAD_TOKEN not set; load harness is opt-in.")
    return tok


@pytest.fixture(scope="session")
def max_sessions(pytestconfig: pytest.Config) -> int:
    return _flag_or_env_int(pytestconfig, "--max-sessions", "LOAD_MAX_SESSIONS", 100)


@pytest.fixture(scope="session")
def duration_s(pytestconfig: pytest.Config) -> int:
    return _flag_or_env_int(pytestconfig, "--duration-s", "LOAD_DURATION_S", 60)


@pytest.fixture(scope="session")
def results_dir(pytestconfig: pytest.Config) -> Path:
    raw = _flag_or_env(
        pytestconfig,
        "--results-dir",
        "LOAD_RESULTS_DIR",
        "tests/load/results",
    )
    p = Path(raw)
    p.mkdir(parents=True, exist_ok=True)
    return p


@pytest.fixture(scope="session")
def run_timestamp() -> str:
    return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())


@pytest.fixture(scope="session")
def host_baseline(results_dir: Path, run_timestamp: str) -> dict:
    """Snapshot the host before any load runs. Written to disk so the
    result is interpretable without re-running."""
    baseline = capture_host_baseline()
    out = results_dir / f"host_baseline_{run_timestamp}.json"
    import json

    out.write_text(json.dumps(baseline, indent=2))
    return baseline
