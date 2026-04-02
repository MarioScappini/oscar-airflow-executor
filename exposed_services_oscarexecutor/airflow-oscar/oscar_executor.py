"""
OSCARExecutor — Custom Airflow 3 Executor that dispatches tasks to an OSCAR
Knative sync service.

Install this as an Airflow plugin or as a Python package, then configure:

    [core]
    executor = oscar_executor.OSCARExecutor

    [oscar]
    endpoint = https://<OSCAR_ENDPOINT>
    service_name = airflow-worker
    service_token = <SERVICE_TOKEN>
    task_timeout = 300
    max_workers = 10
"""

from __future__ import annotations

import json
import logging
from concurrent.futures import Future, ThreadPoolExecutor
from typing import TYPE_CHECKING, Any

from airflow.configuration import conf
from airflow.executors.base_executor import BaseExecutor
from airflow.utils.state import TaskInstanceState

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from airflow.executors import workloads
    from airflow.models.taskinstance import TaskInstanceKey

log = logging.getLogger(__name__)


class OSCARExecutor(BaseExecutor):
    """
    Dispatches Airflow task execution to an OSCAR Knative sync service.

    Each task is sent as a serialized ExecuteTask workload via HTTP POST
    to OSCAR's /run/<service_name> endpoint. The OSCAR service runs:

        python -m airflow.sdk.execution_time.execute_workload --json-path /tmp/workload_input.json

    which uses the Task SDK Supervisor to manage the full task lifecycle
    (Execution API communication, heartbeats, state transitions).
    """

    supports_pickling: bool = False
    supports_sentry: bool = False
    is_local: bool = False
    is_single_threaded: bool = False
    is_production: bool = True
    serve_logs: bool = False
    supports_ad_hoc_ti_run: bool = False

    # Store workloads (not commands) in Airflow 3 style
    queued_tasks: dict[TaskInstanceKey, workloads.All]  # type: ignore[assignment]

    def __init__(self, parallelism: int = 32):
        super().__init__(parallelism=parallelism)

        self._oscar_endpoint = conf.get("oscar", "endpoint")
        self._service_name = conf.get("oscar", "service_name", fallback="airflow-worker")
        self._service_token = conf.get("oscar", "service_token")
        self._task_timeout = conf.getint("oscar", "task_timeout", fallback=300)
        max_workers = conf.getint("oscar", "max_workers", fallback=10)

        self._thread_pool: ThreadPoolExecutor | None = None
        self._max_workers = max_workers

        # Track in-flight futures: TaskInstanceKey -> Future
        self._futures: dict[TaskInstanceKey, Future] = {}

    def start(self) -> None:
        """Start the thread pool for concurrent task dispatch."""
        self._thread_pool = ThreadPoolExecutor(max_workers=self._max_workers)
        log.info(
            "OSCARExecutor started: endpoint=%s, service=%s, max_workers=%d",
            self._oscar_endpoint,
            self._service_name,
            self._max_workers,
        )

    def queue_workload(self, workload: workloads.All, session: Session | None = None) -> None:
        """
        Airflow 3 entry point: queue a workload for execution.

        Called by the scheduler to submit work to the executor.
        """
        from airflow.executors.workloads import ExecuteTask

        if not isinstance(workload, ExecuteTask):
            raise RuntimeError(f"OSCARExecutor cannot handle workloads of type {type(workload)}")

        ti = workload.ti
        self.queued_tasks[ti.key] = workload

    def _process_workloads(self, workload_list: list[workloads.All]) -> None:
        """
        Airflow 3 entry point: process queued workloads.

        Called during the scheduler heartbeat to dispatch queued tasks.
        """
        from airflow.executors.workloads import ExecuteTask

        for w in workload_list:
            if not isinstance(w, ExecuteTask):
                raise RuntimeError(f"OSCARExecutor cannot handle workloads of type {type(w)}")

            key = w.ti.key

            # Remove from queued, add to running
            del self.queued_tasks[key]
            self.running.add(key)

            # Dispatch to OSCAR in a thread
            future = self._thread_pool.submit(self._invoke_oscar, key, w)
            self._futures[key] = future

    def _invoke_oscar(self, key: TaskInstanceKey, workload: workloads.ExecuteTask) -> None:
        """
        Send the serialized workload to the OSCAR service via sync HTTP POST.

        This runs in a thread from the thread pool.
        """
        import requests

        try:
            # Serialize the workload exactly as ECS/K8s executors do
            serialized = workload.model_dump_json()

            payload = {
                "mode": "workload",
                "workload_json": serialized,
                "task_key": {
                    "dag_id": key.dag_id,
                    "task_id": key.task_id,
                    "run_id": key.run_id,
                    "try_number": key.try_number,
                    "map_index": key.map_index,
                },
            }

            log.info("Dispatching task to OSCAR: dag_id=%s, task_id=%s", key.dag_id, key.task_id)

            response = requests.post(
                f"{self._oscar_endpoint}/run/{self._service_name}",
                json=payload,
                headers={"Authorization": f"Bearer {self._service_token}"},
                timeout=self._task_timeout,
            )

            if response.ok:
                log.info("OSCAR task completed successfully: dag_id=%s, task_id=%s", key.dag_id, key.task_id)
                # Note: Task state is already updated by the Task SDK Supervisor
                # via the Execution API inside the OSCAR container.
                # We report success here so the executor removes it from running.
                self.success(key)
            else:
                log.error(
                    "OSCAR task failed: dag_id=%s, task_id=%s, status=%d, body=%s",
                    key.dag_id,
                    key.task_id,
                    response.status_code,
                    response.text[:500],
                )
                self.fail(key)

        except Exception:
            log.exception("Exception dispatching task to OSCAR: dag_id=%s, task_id=%s", key.dag_id, key.task_id)
            self.fail(key)

    def sync(self) -> None:
        """
        Called periodically by the scheduler heartbeat.

        Clean up completed futures and update state.
        """
        completed = []
        for key, future in self._futures.items():
            if future.done():
                completed.append(key)
                # Any exceptions in the future are already handled in _invoke_oscar
                try:
                    future.result()
                except Exception:
                    log.exception("Unhandled exception in OSCAR dispatch for %s", key)

        for key in completed:
            del self._futures[key]

    def end(self) -> None:
        """Wait for all in-flight tasks to complete, then shut down."""
        if self._thread_pool:
            log.info("OSCARExecutor shutting down, waiting for in-flight tasks...")
            self._thread_pool.shutdown(wait=True)

    def terminate(self) -> None:
        """Force shutdown without waiting."""
        if self._thread_pool:
            self._thread_pool.shutdown(wait=False, cancel_futures=True)