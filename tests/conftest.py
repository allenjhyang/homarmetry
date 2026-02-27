"""
Shared fixtures for ClawMetry test suite.
"""
import os
import json
import subprocess
import time
import pytest
import requests


def _detect_gateway_token():
    """Detect gateway token from OpenClaw config."""
    # Environment variable
    token = os.environ.get("CLAWMETRY_TOKEN", "").strip()
    if token:
        return token

    # OpenClaw config
    config_path = os.path.expanduser("~/.openclaw/openclaw.json")
    try:
        with open(config_path) as f:
            cfg = json.load(f)
        token = cfg.get("gateway", {}).get("auth", {}).get("token", "").strip()
        if token:
            return token
    except (FileNotFoundError, ValueError, KeyError):
        pass

    return None


def _is_server_running(base_url, token=None):
    """Check if the ClawMetry server is reachable."""
    try:
        headers = {}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        r = requests.get(f"{base_url}/api/health", headers=headers, timeout=5)
        return r.status_code == 200
    except requests.exceptions.ConnectionError:
        return False


BASE_URL = os.environ.get("CLAWMETRY_URL", "http://localhost:8900")
GATEWAY_TOKEN = _detect_gateway_token()


@pytest.fixture(scope="session")
def base_url():
    return BASE_URL


@pytest.fixture(scope="session")
def token():
    return GATEWAY_TOKEN


@pytest.fixture(scope="session")
def api(base_url, token):
    """Requests session with auth pre-configured."""
    session = requests.Session()
    if token:
        session.headers.update({"Authorization": f"Bearer {token}"})
    return session


@pytest.fixture(scope="session", autouse=True)
def server(base_url, token):
    """Ensure the ClawMetry server is running before tests."""
    if _is_server_running(base_url, token):
        yield base_url
        return

    # Start the server
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    dashboard = os.path.join(repo_root, "dashboard.py")
    proc = subprocess.Popen(
        ["python3", dashboard, "--port", "8900"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    # Wait up to 10 seconds
    for _ in range(20):
        time.sleep(0.5)
        if _is_server_running(base_url, token):
            break
    else:
        proc.terminate()
        pytest.fail("ClawMetry server failed to start")

    yield base_url

    proc.terminate()
