"""Shared in-memory background job registry: a crawl can take minutes, far longer than an MCP tool call or
an HTTP request should stay open for, so it starts a job and returns immediately. Used by both the MCP
server (poll-based `get_run_status`) and the web UI (live progress over Server-Sent Events) — this module
has no dependency on either's transport. Jobs live only for the server process's lifetime (same tradeoff
as `scoutqa serve`'s pairing tokens) — a restarted server has no memory of a job started before it.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Generic, TypeVar

T = TypeVar("T")


class JobStatus(StrEnum):
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass
class JobProgress:
    pages_done: int = 0
    frontier_size: int = 0
    current_url: str = ""


@dataclass
class Job(Generic[T]):
    id: str
    kind: str
    status: JobStatus = JobStatus.RUNNING
    progress: JobProgress = field(default_factory=JobProgress)
    result: T | None = None
    error: str | None = None
    started_at: str = ""
    finished_at: str | None = None
    _subscribers: list[asyncio.Queue[dict[str, Any]]] = field(default_factory=list, repr=False, compare=False)

    def snapshot(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "job_id": self.id, "kind": self.kind, "status": self.status.value,
            "progress": {"pages_done": self.progress.pages_done, "frontier_size": self.progress.frontier_size,
                        "current_url": self.progress.current_url},
            "started_at": self.started_at, "finished_at": self.finished_at,
        }
        if self.status is JobStatus.COMPLETED:
            out["result"] = self.result
        elif self.status is JobStatus.FAILED:
            out["error"] = self.error
        return out

    def update_progress(self, pages_done: int, frontier_size: int, current_url: str) -> None:
        self.progress = JobProgress(pages_done, frontier_size, current_url)
        self._publish()

    def _publish(self) -> None:
        snapshot = self.snapshot()
        for queue in self._subscribers:
            queue.put_nowait(snapshot)


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class JobManager:
    """One per server process."""

    def __init__(self) -> None:
        self._jobs: dict[str, Job[Any]] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}

    def start(self, kind: str, run: Callable[[Job[Any]], Awaitable[T]]) -> str:
        job_id = uuid.uuid4().hex[:12]
        job: Job[T] = Job(id=job_id, kind=kind, started_at=_now())
        self._jobs[job_id] = job

        async def _body() -> None:
            try:
                job.result = await run(job)
                job.status = JobStatus.COMPLETED
            except Exception as exc:  # the job's own failure, surfaced via get_run_status, not raised here
                job.status = JobStatus.FAILED
                job.error = str(exc)
            finally:
                job.finished_at = _now()
                job._publish()

        self._tasks[job_id] = asyncio.create_task(_body())
        return job_id

    def get(self, job_id: str) -> Job[Any] | None:
        return self._jobs.get(job_id)

    async def events(self, job_id: str) -> AsyncIterator[dict[str, Any]]:
        """Live snapshots of `job_id` as they happen, for Server-Sent Events: one immediately, then one
        per progress update, ending with the terminal (completed/failed) snapshot. A job that has already
        finished by the time this is called yields just that one final snapshot."""
        job = self._jobs.get(job_id)
        if job is None:
            return
        yield job.snapshot()
        if job.status is not JobStatus.RUNNING:
            return
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        job._subscribers.append(queue)
        try:
            while True:
                snapshot = await queue.get()
                yield snapshot
                if snapshot["status"] != JobStatus.RUNNING.value:
                    return
        finally:
            job._subscribers.remove(queue)
