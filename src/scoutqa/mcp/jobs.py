"""In-memory background job registry: `crawl_app` can take minutes, far longer than an MCP client is
willing to hold one tool call open, so it starts a job and returns immediately; `get_run_status` polls it.
Jobs live only for the server process's lifetime (same tradeoff as `scoutqa serve`'s pairing tokens) — a
restarted `scoutqa mcp` has no memory of a job started before it.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
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


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class JobManager:
    """One per MCP server process."""

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

        self._tasks[job_id] = asyncio.create_task(_body())
        return job_id

    def get(self, job_id: str) -> Job[Any] | None:
        return self._jobs.get(job_id)
