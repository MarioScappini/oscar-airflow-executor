"""
OSCAR Hook — wraps the OSCAR REST API for synchronous Knative services.

Auth split:
    Management endpoints  →  Basic Auth (oscar user + password)
    /run/{service}        →  Bearer <per-service token>  (no user credentials)
"""
from __future__ import annotations

import logging
from typing import Any

import requests
from airflow.hooks.base import BaseHook

log = logging.getLogger(__name__)


class OSCARHook(BaseHook):
    """
    Thin wrapper around the OSCAR HTTP API.

    Airflow Connection (id: oscar_default):
        schema   : http or https
        host     : OSCAR host
        port     : OSCAR port
        login    : username
        password : password
    """

    conn_name_attr   = "oscar_conn_id"
    default_conn_name = "oscar_default"
    conn_type        = "oscar"
    hook_name        = "OSCAR"

    def __init__(self, oscar_conn_id: str = default_conn_name) -> None:
        super().__init__()
        self.oscar_conn_id = oscar_conn_id
        self._session: requests.Session | None = None

    # ── Internal ──────────────────────────────────────────────────────────────

    def _get_base_url(self) -> str:
        conn   = self.get_connection(self.oscar_conn_id)
        scheme = conn.schema or "http"
        host   = conn.host   or "localhost"
        port   = f":{conn.port}" if conn.port else ""
        return f"{scheme}://{host}{port}"

    def _session_or_new(self) -> requests.Session:
        if self._session is None:
            conn = self.get_connection(self.oscar_conn_id)
            self._session = requests.Session()
            if conn.login:
                self._session.auth = (conn.login, conn.password)
        return self._session

    def _url(self, path: str) -> str:
        return f"{self._get_base_url()}/{path.lstrip('/')}"

    # ── Public API ────────────────────────────────────────────────────────────

    def get_system_info(self) -> dict[str, Any]:
        """GET /system/info"""
        resp = self._session_or_new().get(self._url("/system/info"), timeout=5)
        resp.raise_for_status()
        return resp.json()

    def list_services(self) -> list[dict[str, Any]]:
        """GET /system/services"""
        resp = self._session_or_new().get(self._url("/system/services"), timeout=10)
        resp.raise_for_status()
        return resp.json() or []

    def get_service(self, service_name: str) -> dict[str, Any]:
        """GET /system/services/{name}"""
        resp = self._session_or_new().get(
            self._url(f"/system/services/{service_name}"), timeout=10
        )
        resp.raise_for_status()
        return resp.json()

    def get_service_token(self, service_name: str) -> str:
        """Fetch the per-service Bearer token from the service definition."""
        svc   = self.get_service(service_name)
        token = svc.get("token", "")
        if not token:
            raise ValueError(f"Service '{service_name}' has no token field")
        return token

    def invoke_service_sync(self, service_name: str, payload: dict[str, Any]) -> str:
        """
        POST /run/{service} — synchronous Knative invocation.

        Blocks until the service completes and returns the raw output directly.
        Auth: per-service Bearer token (not user credentials).
        """
        token = self.get_service_token(service_name)
        resp  = requests.post(          # bare requests.post — no session auth
            self._url(f"/run/{service_name}"),
            json=payload,
            headers={"Authorization": f"Bearer {token}"},
            timeout=300,                # generous timeout for long Knative jobs
        )
        resp.raise_for_status()
        log.info("OSCAR sync invocation complete: %s", service_name)
        return resp.text

    def close(self) -> None:
        if self._session:
            self._session.close()
            self._session = None