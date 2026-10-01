"""Shared background job registry (scoutqa.jobs): the poll-based snapshot MCP uses and the SSE-style
`events()` stream the web UI uses are two views of the same underlying job — both must agree.
"""

from __future__ import annotations

import asyncio

from scoutqa.jobs import Job, JobManager, JobStatus


async def test_job_completes_and_snapshot_reflects_the_result() -> None:
    jobs = JobManager()

    async def run(_job: Job[dict[str, int]]) -> dict[str, int]:
        return {"pages": 3}

    job_id = jobs.start("crawl", run)
    for _ in range(50):
        if jobs.get(job_id).status is not JobStatus.RUNNING:  # type: ignore[union-attr]
            break
        await asyncio.sleep(0.01)
    snapshot = jobs.get(job_id).snapshot()  # type: ignore[union-attr]
    assert snapshot["status"] == "completed" and snapshot["result"] == {"pages": 3}
    assert "error" not in snapshot


async def test_job_failure_is_captured_not_raised() -> None:
    jobs = JobManager()

    async def run(_job: Job[None]) -> None:
        raise RuntimeError("boom")

    job_id = jobs.start("crawl", run)
    for _ in range(50):
        if jobs.get(job_id).status is not JobStatus.RUNNING:  # type: ignore[union-attr]
            break
        await asyncio.sleep(0.01)
    snapshot = jobs.get(job_id).snapshot()  # type: ignore[union-attr]
    assert snapshot["status"] == "failed" and snapshot["error"] == "boom"
    assert "result" not in snapshot


async def test_get_unknown_job_returns_none() -> None:
    assert JobManager().get("nope") is None


async def test_events_yields_an_empty_stream_for_an_unknown_job() -> None:
    events = [e async for e in JobManager().events("nope")]
    assert events == []


async def test_events_on_an_already_finished_job_yields_only_the_final_snapshot() -> None:
    jobs = JobManager()

    async def run(_job: Job[str]) -> str:
        return "done"

    job_id = jobs.start("crawl", run)
    await asyncio.sleep(0.05)  # let it finish before anyone subscribes
    assert jobs.get(job_id).status is JobStatus.COMPLETED  # type: ignore[union-attr]

    events = [e async for e in jobs.events(job_id)]
    assert len(events) == 1 and events[0]["status"] == "completed" and events[0]["result"] == "done"


async def test_events_streams_progress_then_the_terminal_snapshot() -> None:
    jobs = JobManager()
    started = asyncio.Event()
    proceed = asyncio.Event()

    async def run(job: Job[str]) -> str:
        job.update_progress(1, 5, "https://x.io/a")
        started.set()
        await proceed.wait()
        job.update_progress(2, 5, "https://x.io/b")
        return "done"

    job_id = jobs.start("crawl", run)
    await started.wait()

    seen = []

    async def collect() -> None:
        async for event in jobs.events(job_id):
            seen.append(event)

    task = asyncio.create_task(collect())
    await asyncio.sleep(0.02)  # let the subscriber register and receive the initial snapshot
    proceed.set()
    await asyncio.wait_for(task, timeout=2)

    statuses = [e["status"] for e in seen]
    assert statuses[-1] == "completed"
    assert seen[-1]["result"] == "done"
    urls = [e["progress"]["current_url"] for e in seen]
    assert "https://x.io/b" in urls  # the second update, published after the subscriber joined, arrived
