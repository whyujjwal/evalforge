"""End-to-end engine tests against the mock provider."""

from __future__ import annotations

import anyio
import pytest
from evalforge import (
    Engine,
    EventBus,
    Suite,
    Task,
    llm_agent,
    llm_judge,
    rule_judge,
)
from evalforge.errors import ProviderPermanentError, ProviderTransientError
from evalforge.events import collect_events
from evalforge.providers import clear_registry
from evalforge.providers.mock import install_mock_provider
from evalforge.types import RunStatus, TaskStatus


@pytest.fixture(autouse=True)
def _reset_registry():
    clear_registry()
    yield
    clear_registry()


def _suite(
    *,
    script: list[object] | None = None,
    rubric: str = "score: 1.0",
    tasks: list[Task] | None = None,
    include_rule: bool = True,
) -> Suite:
    install_mock_provider(script=script or [rubric])
    solver = llm_agent(
        "solver",
        provider="mock",
        model="mock",
        prompt="Solve: {question}",
    )
    judge = llm_judge(
        "correctness",
        provider="mock",
        model="mock",
        rubric="score based on {output}",
    )
    stage2: list[object] = [judge]
    if include_rule:
        stage2.append(rule_judge("format", lambda _v: True))
    return Suite(
        name="s",
        tasks=tasks
        or [
            Task(id="t1", input={"question": "1+1"}, expected_output="2"),
            Task(id="t2", input={"question": "2+2"}, expected_output="4"),
        ],
        pipeline=[solver, stage2],
    )


async def test_engine_happy_path() -> None:
    suite = _suite(script=["42", "score: 0.9 good"])
    bus = EventBus(buffer_size=256)
    engine = Engine(bus=bus, own_bus=True)
    async with collect_events(bus) as seen:
        result = await engine.run(suite)

    run = result.run
    assert run.status is RunStatus.COMPLETED
    assert len(run.results) == 2
    for r in run.results:
        assert r.status is TaskStatus.OK
        assert r.scores  # at least one score
    assert {e.kind for e in seen} >= {
        "run_started",
        "task_started",
        "agent_started",
        "agent_completed",
        "judge_started",
        "judge_completed",
        "task_finished",
        "run_finished",
    }
    # Seq is monotonic.
    seqs = [e.seq for e in seen]
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)


async def test_engine_concurrency_bounded() -> None:
    # With concurrency=2 and latency=0.1s on 6 tasks, total time must be
    # around 0.3s (3 waves of 2). If the semaphore was broken, ~0.1s.
    install_mock_provider(latency_s=0.05, script=["ok"])
    solver = llm_agent("solver", provider="mock", model="mock", prompt="Solve: {question}")
    tasks = [Task(id=f"t{i}", input={"question": str(i)}) for i in range(6)]
    from evalforge.types import RunConfig

    suite = Suite(
        name="s",
        tasks=tasks,
        pipeline=[solver, rule_judge("ok", lambda _v: True)],
        config=RunConfig(concurrency=2),
    )
    engine = Engine()
    t0 = anyio.current_time()
    result = await engine.run(suite)
    elapsed = anyio.current_time() - t0
    assert result.run.status is RunStatus.COMPLETED
    assert elapsed >= 0.10, f"elapsed={elapsed:.3f}s — concurrency not bounded"


async def test_transient_errors_retry_then_succeed() -> None:
    install_mock_provider(
        script=[ProviderTransientError("blip"), ProviderTransientError("blip"), "ok"]
    )
    solver = llm_agent("solver", provider="mock", model="mock", prompt="Solve: {question}")
    suite = Suite(
        name="s",
        tasks=[Task(id="t1", input={"question": "?"})],
        pipeline=[solver, rule_judge("ok", lambda _v: True)],
    )
    engine = Engine()
    result = await engine.run(suite)
    assert result.run.status is RunStatus.COMPLETED
    assert result.run.results[0].status is TaskStatus.OK
    assert result.run.results[0].attempts >= 3


async def test_permanent_error_fails_task_not_run() -> None:
    install_mock_provider(script=[ProviderPermanentError("bad key")])
    solver = llm_agent("solver", provider="mock", model="mock", prompt="Solve: {question}")
    suite = Suite(
        name="s",
        tasks=[
            Task(id="t1", input={"question": "?"}),
            Task(id="t2", input={"question": "?"}),
        ],
        pipeline=[solver, rule_judge("ok", lambda _v: True)],
    )
    # Second task needs a valid response, but the mock loops the script. We
    # want every task to fail here, and the run to still complete.
    engine = Engine()
    result = await engine.run(suite)
    assert result.run.status is RunStatus.COMPLETED
    for r in result.run.results:
        assert r.status is TaskStatus.FAILED
        assert "PermanentError" in (r.error or "")


async def test_graceful_shutdown_via_stop_event() -> None:
    install_mock_provider(latency_s=0.05, script=["ok"])
    solver = llm_agent("solver", provider="mock", model="mock", prompt="Solve: {question}")
    tasks = [Task(id=f"t{i}", input={"question": "q"}) for i in range(10)]
    from evalforge.types import RunConfig

    suite = Suite(
        name="s",
        tasks=tasks,
        pipeline=[solver, rule_judge("ok", lambda _v: True)],
        config=RunConfig(concurrency=2),
    )
    engine = Engine()
    stop = anyio.Event()

    async def trigger_stop() -> None:
        await anyio.sleep(0.05)
        stop.set()

    async with anyio.create_task_group() as tg:
        tg.start_soon(trigger_stop)
        result = await engine.run(suite, stop_event=stop)

    assert result.run.status is RunStatus.CANCELLED
    # Some tasks completed, some were skipped.
    assert 0 < len(result.run.results) < 10
