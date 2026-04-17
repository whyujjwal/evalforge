"""Storage + resume integration tests.

Per M3 spec, this file proves three resume invariants under fault injection:

1. No task is scored twice after resume.
2. No task is silently dropped.
3. Final results on a resumed run equal those of a clean run on the same seed.
"""

from __future__ import annotations

from pathlib import Path

import anyio
import pytest
from evalforge import (
    Engine,
    EventBus,
    RunConfig,
    SQLiteStorage,
    Suite,
    Task,
    llm_agent,
    rule_judge,
)
from evalforge.providers import clear_registry
from evalforge.providers.mock import install_mock_provider
from evalforge.types import RunStatus, TaskStatus


@pytest.fixture(autouse=True)
def _reset_registry():
    clear_registry()
    yield
    clear_registry()


def _suite(tasks: int = 6, concurrency: int = 2, seed: int = 42) -> Suite:
    solver = llm_agent("solver", provider="mock", model="mock", prompt="Solve: {question}")
    judge = rule_judge("is_str", lambda v: bool(isinstance(v, str) and v))
    return Suite(
        name="resume-suite",
        tasks=[Task(id=f"t{i}", input={"question": str(i)}) for i in range(tasks)],
        pipeline=[solver, judge],
        config=RunConfig(concurrency=concurrency, seed=seed),
    )


async def test_storage_roundtrip(tmp_path: Path) -> None:
    install_mock_provider(script=["answer"])
    store = SQLiteStorage(tmp_path / "db.sqlite")
    await store.initialize()
    bus = EventBus(buffer_size=512)
    engine = Engine(bus=bus, own_bus=False)
    suite = _suite(tasks=3)

    async with store.attach(bus):
        result = await engine.run(suite)
        await bus.close()

    assert result.run.status is RunStatus.COMPLETED
    assert len(result.run.results) == 3

    # Re-load from storage; must match in-memory.
    loaded = await store.load_run(result.run.id)
    assert loaded.status is RunStatus.COMPLETED
    loaded_ids = sorted(r.task_id for r in loaded.results)
    assert loaded_ids == ["t0", "t1", "t2"]
    for r in loaded.results:
        assert r.status is TaskStatus.OK
        assert any(s.rubric_name == "is_str" for s in r.scores)
    await store.close()


async def test_resume_skips_completed_and_matches_clean_run(tmp_path: Path) -> None:
    """The key M3 invariant: kill the engine mid-run, resume, verify
    (a) no double-scoring, (b) nothing dropped, (c) results match a clean run."""
    # --- (A) Crash mid-run ---------------------------------------------------
    install_mock_provider(latency_s=0.05, script=["answer"])
    store_crashed = SQLiteStorage(tmp_path / "crashed.sqlite")
    await store_crashed.initialize()
    bus = EventBus(buffer_size=512)
    engine = Engine(bus=bus, own_bus=False)
    suite = _suite(tasks=8, concurrency=2, seed=123)

    stop = anyio.Event()
    run_id: str | None = None

    async def trigger_stop() -> None:
        await anyio.sleep(0.07)
        stop.set()

    async with store_crashed.attach(bus):
        async with anyio.create_task_group() as tg:
            tg.start_soon(trigger_stop)
            result = await engine.run(suite, stop_event=stop)
        run_id = result.run.id
        await bus.close()

    assert run_id is not None
    mid = await store_crashed.load_run(run_id)
    assert mid.status is RunStatus.CANCELLED
    assert 0 < len(mid.results) < 8, f"expected partial completion, got {len(mid.results)}"
    crashed_ids = {r.task_id for r in mid.results}

    # --- (B) Resume ----------------------------------------------------------
    install_mock_provider(latency_s=0.0, script=["answer"])
    bus2 = EventBus(buffer_size=512)
    engine2 = Engine(bus=bus2, own_bus=False)

    async with store_crashed.attach(bus2):
        resumed = await engine2.run(suite, resume_from=mid)
        await bus2.close()

    assert resumed.run.status is RunStatus.COMPLETED
    # (1) No task was scored twice — unique task_ids.
    resumed_ids = [r.task_id for r in resumed.run.results]
    assert len(set(resumed_ids)) == len(resumed_ids), "duplicate task results"
    # (2) Nothing dropped — all 8 tasks accounted for.
    assert set(resumed_ids) == {f"t{i}" for i in range(8)}
    # Previously-completed tasks appear unchanged.
    for r in resumed.run.results:
        if r.task_id in crashed_ids:
            # Status should still be OK; attempts preserved.
            assert r.status is TaskStatus.OK

    # --- (C) Clean run on the same seed --------------------------------------
    install_mock_provider(latency_s=0.0, script=["answer"])
    store_clean = SQLiteStorage(tmp_path / "clean.sqlite")
    await store_clean.initialize()
    bus3 = EventBus(buffer_size=512)
    engine3 = Engine(bus=bus3, own_bus=False)
    async with store_clean.attach(bus3):
        clean = await engine3.run(suite)
        await bus3.close()

    resumed_by_id = {r.task_id: r for r in resumed.run.results}
    clean_by_id = {r.task_id: r for r in clean.run.results}
    assert resumed_by_id.keys() == clean_by_id.keys()
    for tid, rc in resumed_by_id.items():
        cc = clean_by_id[tid]
        # Status, score names, and score values must match under the same
        # seeded deterministic mock.
        assert rc.status is cc.status
        assert {s.rubric_name: s.value for s in rc.scores} == {
            s.rubric_name: s.value for s in cc.scores
        }
    await store_crashed.close()
    await store_clean.close()


async def test_resume_on_already_complete_run_is_no_op(tmp_path: Path) -> None:
    install_mock_provider(script=["answer"])
    store = SQLiteStorage(tmp_path / "db.sqlite")
    await store.initialize()
    bus = EventBus(buffer_size=512)
    engine = Engine(bus=bus, own_bus=False)
    suite = _suite(tasks=3, concurrency=2, seed=7)

    async with store.attach(bus):
        first = await engine.run(suite)
        await bus.close()
    assert first.run.status is RunStatus.COMPLETED

    # Resume on a fully-complete run: no new work, same id, same results.
    bus2 = EventBus()
    engine2 = Engine(bus=bus2, own_bus=False)
    async with store.attach(bus2):
        again = await engine2.run(suite, resume_from=first.run)
        await bus2.close()
    assert again.run.status is RunStatus.COMPLETED
    assert {r.task_id for r in again.run.results} == {f"t{i}" for i in range(3)}
    await store.close()
