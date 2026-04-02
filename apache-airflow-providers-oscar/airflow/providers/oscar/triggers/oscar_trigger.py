"""
OSCARJobTrigger — async trigger that defers until an OSCAR job finishes.
Used by deferrable operators / sensors running on the Triggerer component.
"""
from __future__ import annotations

import asyncio
from typing import Any, AsyncIterator

from airflow.triggers.base import BaseTrigger, TriggerEvent


class OSCARJobTrigger(BaseTrigger):
    def __init__(
        self,
        job_id: str,
        oscar_conn_id: str = "oscar_default",
        poll_interval: float = 5.0,
    ) -> None:
        super().__init__()
        self.job_id        = job_id
        self.oscar_conn_id = oscar_conn_id
        self.poll_interval = poll_interval

    def serialize(self) -> tuple[str, dict[str, Any]]:
        return (
            "airflow.providers.oscar.triggers.oscar_trigger.OSCARJobTrigger",
            {
                "job_id":        self.job_id,
                "oscar_conn_id": self.oscar_conn_id,
                "poll_interval": self.poll_interval,
            },
        )

    async def run(self) -> AsyncIterator[TriggerEvent]:
        from airflow.providers.oscar.hooks.oscar_hook import OSCARHook

        hook = OSCARHook(oscar_conn_id=self.oscar_conn_id)

        while True:
            status = await hook.async_get_job_status(self.job_id)

            if status in ("DONE", "ERROR"):
                yield TriggerEvent({
                    "status": status,
                    "job_id": self.job_id,
                })
                return

            await asyncio.sleep(self.poll_interval)