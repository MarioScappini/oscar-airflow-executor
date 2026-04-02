"""
Integration test for OSCARHook against a real OSCAR instance.
Tests synchronous Knative services only — no job_id or polling.

Usage:
    pip install requests

    OSCAR_HOST=localhost OSCAR_PORT=8080 \
    OSCAR_USER=oscar OSCAR_PASS=secret \
    OSCAR_SERVICE=my-service \
    python test_oscar_hook.py
"""
from __future__ import annotations

import base64
import logging
import os
import sys

import requests

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)
log = logging.getLogger("oscar.integration")

OSCAR_HOST    = os.getenv("OSCAR_HOST",    "localhost")
OSCAR_PORT    = int(os.getenv("OSCAR_PORT", "8080"))
OSCAR_USER    = os.getenv("OSCAR_USER",    "oscar")
OSCAR_PASS    = os.getenv("OSCAR_PASS",    "YmI5MDkz")
OSCAR_SERVICE = os.getenv("OSCAR_SERVICE", "hub-vijure-zhvv3ybk")
BASE_URL      = f"http://{OSCAR_HOST}:{OSCAR_PORT}"


class _OSCARClient:
    """Mirrors OSCARHook exactly — no Airflow dependency."""

    def __init__(self, base_url: str, user: str, password: str) -> None:
        self.base_url  = base_url.rstrip("/")
        self._session  = requests.Session()
        self._session.auth = (user, password)
        encoded = base64.b64encode(f"{user}:{password}".encode()).decode()
        log.debug("Basic auth: %s", encoded)

    def _url(self, path: str) -> str:
        return f"{self.base_url}/{path.lstrip('/')}"

    def get_system_info(self) -> dict:
        resp = self._session.get(self._url("/system/info"), timeout=5)
        resp.raise_for_status()
        return resp.json()

    def list_services(self) -> list:
        resp = self._session.get(self._url("/system/services"), timeout=10)
        resp.raise_for_status()
        return resp.json() or []

    def get_service(self, service_name: str) -> dict:
        resp = self._session.get(self._url(f"/system/services/{service_name}"), timeout=10)
        resp.raise_for_status()
        return resp.json()

    def get_service_token(self, service_name: str) -> str:
        token = self.get_service(service_name).get("token", "")
        if not token:
            raise ValueError(f"Service '{service_name}' has no token field")
        return token

    def invoke_service_sync(self, service_name: str, payload: dict) -> str:
        """POST /run/{service} — blocks, returns raw output. Bearer token only."""
        token = self.get_service_token(service_name)
        resp  = requests.post(      # bare post — no session auth
            self._url(f"/run/{service_name}"),
            json=payload,
            headers={"Authorization": f"Bearer {token}"},
            timeout=300,
        )
        resp.raise_for_status()
        return resp.text

    def close(self) -> None:
        self._session.close()


# ── Test helpers ──────────────────────────────────────────────────────────────

_client: _OSCARClient | None = None
PASS_SYM = "\033[92m✓\033[0m"
FAIL_SYM = "\033[91m✗\033[0m"
_results: list[tuple[str, bool, str]] = []


def run_test(name: str, fn) -> bool:
    try:
        fn()
        _results.append((name, True, ""))
        print(f"  {PASS_SYM}  {name}")
        return True
    except Exception as exc:
        _results.append((name, False, str(exc)))
        print(f"  {FAIL_SYM}  {name}")
        print(f"        {exc}")
        return False


# ── Tests ─────────────────────────────────────────────────────────────────────

def test_connectivity():
    """GET /system/info — reachable and Basic Auth accepted."""
    data = _client.get_system_info()
    log.info("System info: %s", data)
    assert "version" in data or "name" in data

def test_list_services():
    """GET /system/services — returns a list."""
    services = _client.list_services()
    log.info("Services: %s", [s.get("name") for s in services])
    assert isinstance(services, list)

def test_service_exists():
    """GET /system/services/{name} — service is deployed and has a token."""
    svc = _client.get_service(OSCAR_SERVICE)
    log.info("Service: name=%s token=%s...", svc.get("name"), str(svc.get("token",""))[:8])
    assert svc.get("name") == OSCAR_SERVICE
    assert svc.get("token"), "Service has no token — is it deployed correctly?"

def test_invoke_sync():
    """POST /run/{service} — blocks, returns non-empty output."""
    output = _client.invoke_service_sync(OSCAR_SERVICE, payload={})
    log.info("Output: %s", output[:200])
    assert isinstance(output, str) and len(output) > 0, f"Empty output from service"

def test_bad_credentials():
    """Wrong password → 401."""
    resp = requests.get(f"{BASE_URL}/system/info", auth=("wrong", "credentials"), timeout=5)
    assert resp.status_code == 401, f"Expected 401, got {resp.status_code}"

def test_close():
    """close() cleans up without error."""
    tmp = _OSCARClient(BASE_URL, OSCAR_USER, OSCAR_PASS)
    tmp.get_system_info()
    tmp.close()


# ── Runner ────────────────────────────────────────────────────────────────────

def main():
    global _client

    print(f"\n{'─'*60}")
    print(f"  OSCAR Hook Integration Tests  (sync Knative)")
    print(f"  Target  : {BASE_URL}")
    print(f"  User    : {OSCAR_USER}")
    print(f"  Service : {OSCAR_SERVICE}")
    print(f"{'─'*60}\n")

    _client = _OSCARClient(BASE_URL, OSCAR_USER, OSCAR_PASS)

    tests = [
        ("Connectivity (GET /system/info)",            test_connectivity),
        ("List services (GET /system/services)",        test_list_services),
        (f"Service exists ({OSCAR_SERVICE})",           test_service_exists),
        ("Sync invocation (POST /run/:svc)",            test_invoke_sync),
        ("Bad credentials → 401",                      test_bad_credentials),
        ("close() cleans up",                          test_close),
    ]

    for name, fn in tests:
        run_test(name, fn)

    _client.close()

    passed = sum(1 for _, ok, _ in _results if ok)
    failed = len(_results) - passed

    print(f"\n{'─'*60}")
    print(f"  {passed}/{len(_results)} passed", end="")
    if failed:
        print(f"  — {failed} failed")
        for name, ok, err in _results:
            if not ok:
                print(f"    {FAIL_SYM} {name}: {err}")
    else:
        print(f"  {PASS_SYM}")
    print(f"{'─'*60}\n")

    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()