"""
OSCAR Executor — runs Airflow tasks as synchronous Knative invocations.

Each TaskInstance becomes a POST /run/{service} call.
Since /run/ blocks until the service completes and returns output directly,
there is no job_id and no polling loop — the task is marked success or
failure as soon as the HTTP response comes back.

executor_config per task:
    oscar_service (str) : OSCAR Knative service name to invoke  [required]
    oscar_conn_id (str) : Airflow connection id                 [default: oscar_default]
"""
from __future__ import annotations

import concurrent.futures
import logging
from typing import Any

from airflow.executors.base_executor import BaseExecutor
from airflow.models.taskinstancekey import TaskInstanceKey

log = logging.getLogger(__name__)


class OSCARExecutor(BaseExecutor):
    """
    Airflow executor that runs tasks via OSCAR synchronous Knative services.

    execute_async() submits each task to a thread that calls POST /run/{service}
    and blocks until Knative returns. The result (success or failure) is
    reported back to the scheduler immediately — no sync() polling needed.
    """

    supports_pickling: bool = False

    def __init__(self, parallelism: int = 32) -> None:
        super().__init__(parallelism=parallelism)
        # Thread pool so the scheduler heartbeat is never blocked by a
        # long-running Knative call.
        self._pool = concurrent.futures.ThreadPoolExecutor(
            max_workers=parallelism,
            thread_name_prefix="oscar-exec",
        )
        # key → Future, so we can cancel on terminate()
        self._futures: dict[TaskInstanceKey, concurrent.futures.Future] = {}

    # ── BaseExecutor interface ────────────────────────────────────────────────

    def start(self) -> None:
        log.info("OSCARExecutor started (parallelism=%d)", self.parallelism)

    def execute_async(
        self,
        key: TaskInstanceKey,
        command: list[str],
        queue: str | None = None,
        executor_config: dict[str, Any] | None = None,
    ) -> None:
        """
        Dispatch a task to the thread pool.
        Each thread calls POST /run/{service} and blocks until Knative responds.
        """
        cfg          = executor_config or {}
        service_name = cfg.get("oscar_service", "airflow-default")
        conn_id      = cfg.get("oscar_conn_id", "oscar_default")

        payload = {
            "airflow_command": command,
            "task_key": {
                "dag_id":    key.dag_id,
                "task_id":   key.task_id,
                "run_id":    key.run_id,
                "map_index": key.map_index,
            },
        }

        future = self._pool.submit(
            self._run_sync, key, service_name, conn_id, payload
        )
        self._futures[key] = future
        log.info("Task %s dispatched to OSCAR service '%s'", key, service_name)

    def _run_sync(
        self,
        key: TaskInstanceKey,
        service_name: str,
        conn_id: str,
        payload: dict[str, Any],
    ) -> None:
        """
        Blocking call executed inside a worker thread.
        Calls POST /run/{service} and reports success/failure to the scheduler.
        """
        from airflow.providers.oscar.hooks.oscar_hook import OSCARHook

        hook = OSCARHook(oscar_conn_id=conn_id)
        try:
            output = hook.invoke_service_sync(
                service_name=service_name,
                payload=payload,
            )
            log.info("Task %s completed. Output: %s", key, output[:200])
            self.success(key)
        except Exception as exc:
            log.error("Task %s failed on OSCAR service '%s': %s", key, service_name, exc)
            self.fail(key)
        finally:
            hook.close()

    def sync(self) -> None:
        """
        Called by the scheduler heartbeat.
        With sync Knative services there is nothing to poll — results are
        reported directly from the worker threads as they complete.
        Clean up any finished futures to avoid memory growth.
        """
        done = [k for k, f in self._futures.items() if f.done()]
        for k in done:
            self._futures.pop(k, None)

    def end(self) -> None:
        log.info("OSCARExecutor shutting down, waiting for %d in-flight tasks...", len(self._futures))
        self._pool.shutdown(wait=True)

    def terminate(self) -> None:
        log.warning("OSCARExecutor terminating, cancelling %d in-flight tasks.", len(self._futures))
        for f in self._futures.values():
            f.cancel()
        self._pool.shutdown(wait=False)