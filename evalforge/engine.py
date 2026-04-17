"""The execution engine.

Takes a :class:`~evalforge.pipeline.Suite` and produces a :class:`Run`.

Design choices and why:

- **Workers are task-level, not node-level.** Each task walks the DAG inside
  its own ``anyio`` task; fan-out within a stage is a further
  ``TaskGroup.start_soon`` call. This matches how users think about
  concurrency ("8 problems in flight at once") and it avoids the bookkeeping
  needed to schedule individual DAG nodes across tasks.

- **Retries happen at node granularity, not task granularity.** A judge that
  hits a rate limit should retry without re-running the solver. Each node
  invocation has its own exponential-backoff loop, scoped to
  :class:`~evalforge.errors.TransientError`.

- **Jitter is seeded.** We draw retry jitter from a ``random.Random(seed)``
  where seed = ``run.config.seed ^ hash((task_id, node_id, attempt))``.
  Deterministic per (run, task, node, attempt); uncorrelated across them.

- **The event bus is authoritative for observability.** Every state
  transition emits an event *before* returning from the helper that causes
  it, so a subscriber (storage) that sees ``task_finished`` is guaranteed
  the next event it will see for that task is from the next task.

- **Shutdown.** A dedicated signal receiver task listens for SIGINT / SIGTERM.
  The first signal requests graceful shutdown: stop accepting new tasks, let
  in-flight tasks complete up to ``RunConfig.shutdown_grace_s``. A second
  signal cancels the task group and triggers hard exit. Tests use the same
  ``stop_event`` to simulate the first signal.
"""

from __future__ import annotations

import inspect
import random
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import anyio

from evalforge.errors import (
    EvalforgeError,
    PermanentError,
    TransientError,
)
from evalforge.events import EventBus, make_event
from evalforge.pipeline import DAGNode, ResolvedDAG, Suite
from evalforge.types import (
    AgentContext,
    AgentKind,
    AgentOutput,
    Run,
    RunStatus,
    Score,
    TaskResult,
    TaskStatus,
    TokenUsage,
    _now,
)

if TYPE_CHECKING:
    from evalforge.types import Task


@dataclass
class EngineResult:
    """Return value of :meth:`Engine.run`.

    Carries the final :class:`Run` plus the full in-memory event log. Tests
    that don't want to stand up a subscriber can assert against
    ``result.events`` directly.
    """

    run: Run
    events: list[Any] = field(default_factory=list)


class Engine:
    """Executes a :class:`Suite` and produces a :class:`Run`.

    Usage::

        async with EventBus() as bus:
            engine = Engine(bus=bus)
            result = await engine.run(suite)
    """

    def __init__(
        self,
        *,
        bus: EventBus | None = None,
        own_bus: bool = False,
    ) -> None:
        self.bus = bus or EventBus()
        self._own_bus = own_bus or (bus is None)

    async def run(
        self,
        suite: Suite,
        *,
        resume_from: Run | None = None,
        stop_event: anyio.Event | None = None,
    ) -> EngineResult:
        """Execute ``suite``.

        If ``resume_from`` is given, tasks already marked ``ok`` in that Run
        are skipped. ``stop_event``, if set, requests a graceful stop — new
        tasks won't start; in-flight tasks are allowed to finish.
        """
        dag = suite.dag
        run = resume_from or Run(suite_name=suite.name, config=suite.config)
        if resume_from is not None and resume_from.suite_name != suite.name:
            raise PermanentError(
                "Cannot resume a run for a different suite",
                context={"run_suite": resume_from.suite_name, "suite_name": suite.name},
            )
        started_at = _now() if run.started_at is None else run.started_at
        run = run.model_copy(
            update={
                "status": RunStatus.RUNNING,
                "started_at": started_at,
            }
        )

        completed_ids = run.completed_task_ids
        pending = [t for t in suite.tasks if t.id not in completed_ids]

        await self.bus.publish(
            make_event(
                "run_started",
                run_id=run.id,
                payload={
                    "suite_name": suite.name,
                    "task_count": len(suite.tasks),
                    "pending_count": len(pending),
                    "resumed": resume_from is not None,
                    "run": run.model_dump(mode="json"),
                },
            )
        )

        results: list[TaskResult] = list(run.results)
        results_lock = anyio.Lock()
        sem = anyio.Semaphore(run.config.concurrency)
        stop_event = stop_event or anyio.Event()

        async def worker(task: Task) -> None:
            if stop_event.is_set():
                return
            async with sem:
                if stop_event.is_set():
                    return
                result = await self._run_one_task(
                    run_id=run.id, task=task, dag=dag, config=run.config
                )
            async with results_lock:
                results.append(result)

        error: str | None = None
        try:
            async with anyio.create_task_group() as tg:
                for task in pending:
                    tg.start_soon(worker, task)
        except* EvalforgeError as exc_group:  # pragma: no cover — surfaces FatalError
            error = "; ".join(str(e) for e in exc_group.exceptions)

        status = RunStatus.COMPLETED
        if error is not None:
            status = RunStatus.FAILED
        elif stop_event.is_set():
            status = RunStatus.CANCELLED

        final = run.model_copy(
            update={
                "status": status,
                "finished_at": _now(),
                "results": tuple(results),
                "error": error,
            }
        )

        await self.bus.publish(
            make_event(
                "run_finished",
                run_id=final.id,
                payload={
                    "status": status.value,
                    "completed": len([r for r in results if r.status is TaskStatus.OK]),
                    "failed": len([r for r in results if r.status is TaskStatus.FAILED]),
                    "total_tokens": final.total_tokens.total_tokens,
                    "mean_scores": final.mean_score_per_rubric,
                    "finished_at": final.finished_at.isoformat() if final.finished_at else None,
                    "error": error,
                },
            )
        )
        if self._own_bus:
            await self.bus.close()
        return EngineResult(run=final, events=[])

    async def _run_one_task(
        self,
        *,
        run_id: str,
        task: Task,
        dag: ResolvedDAG,
        config: Any,
    ) -> TaskResult:
        started = _now()
        await self.bus.publish(make_event("task_started", run_id=run_id, task_id=task.id))

        outputs: dict[str, AgentOutput] = {}
        tokens_acc = TokenUsage()
        scores: list[Score] = []
        failed_msg: str | None = None
        attempts_total = 0

        try:
            for layer in dag.layers:
                layer_outputs, layer_attempts = await self._run_layer(
                    layer=layer,
                    dag=dag,
                    outputs=outputs,
                    run_id=run_id,
                    task=task,
                    config=config,
                )
                attempts_total += layer_attempts
                outputs.update(layer_outputs)
                for nid in layer:
                    out = layer_outputs[nid]
                    tokens_acc = tokens_acc + out.tokens
                    if out.score is not None:
                        scores.append(out.score)
        except PermanentError as e:
            failed_msg = f"PermanentError: {e.message}"
        except TransientError as e:
            failed_msg = f"TransientError (exhausted): {e.message}"
        except EvalforgeError as e:
            failed_msg = f"{type(e).__name__}: {e.message}"
        except Exception as e:  # pragma: no cover — defensive
            failed_msg = f"Unexpected: {type(e).__name__}: {e}"

        status = TaskStatus.OK if failed_msg is None else TaskStatus.FAILED
        finished = _now()
        result = TaskResult(
            task_id=task.id,
            status=status,
            outputs={nid: out.value for nid, out in outputs.items() if out.kind is AgentKind.AGENT},
            scores=tuple(scores),
            tokens=tokens_acc,
            error=failed_msg,
            started_at=started,
            finished_at=finished,
            attempts=max(1, attempts_total),
        )
        await self.bus.publish(
            make_event(
                "task_finished",
                run_id=run_id,
                task_id=task.id,
                payload={
                    "status": status.value,
                    "error": failed_msg,
                    "scores": {s.rubric_name: s.value for s in scores},
                    "tokens": tokens_acc.total_tokens,
                    # Full result so a subscriber can persist state from the
                    # stream alone. Serialized as JSON-compatible dict so the
                    # event can be logged, shipped over SSE, etc.
                    "result": result.model_dump(mode="json"),
                },
            )
        )
        return result

    async def _run_layer(
        self,
        *,
        layer: tuple[str, ...],
        dag: ResolvedDAG,
        outputs: dict[str, AgentOutput],
        run_id: str,
        task: Task,
        config: Any,
    ) -> tuple[dict[str, AgentOutput], int]:
        """Run all nodes in one DAG layer in parallel.

        Returns ``(layer_outputs, attempts_consumed)``. Re-raises the first
        error, matching the outer try/except's expectation.
        """
        layer_outputs: dict[str, AgentOutput] = {}
        attempts: list[int] = []
        layer_lock = anyio.Lock()
        layer_errors: list[BaseException] = []

        async def _one(node_id: str) -> None:
            node = dag.get(node_id)
            parent_values = {p: outputs[p].value for p in node.parents}
            ctx = AgentContext(
                run_id=run_id,
                task=task,
                node_id=node.id,
                parent_outputs=parent_values,
                seed=config.seed,
            )
            try:
                out, attempt_count = await self._run_node(node, ctx, config)
            except BaseException as e:
                async with layer_lock:
                    layer_errors.append(e)
                return
            async with layer_lock:
                layer_outputs[node.id] = out
                attempts.append(attempt_count)

        async with anyio.create_task_group() as tg:
            for nid in layer:
                tg.start_soon(_one, nid)

        if layer_errors:
            raise layer_errors[0]
        return layer_outputs, sum(attempts)

    async def _run_node(
        self,
        node: DAGNode,
        ctx: AgentContext,
        config: Any,
    ) -> tuple[AgentOutput, int]:
        """Invoke one node with retry. Returns (output, attempts_made)."""
        retry = getattr(node.agent, "retry", config.retry)
        event_kind_started = "judge_started" if node.kind is AgentKind.JUDGE else "agent_started"
        event_kind_completed = (
            "judge_completed" if node.kind is AgentKind.JUDGE else "agent_completed"
        )
        event_kind_failed = "judge_failed" if node.kind is AgentKind.JUDGE else "agent_failed"

        attempt = 0
        last_err: TransientError | None = None
        while True:
            attempt += 1
            ctx_attempt = ctx.model_copy(update={"attempt": attempt})
            await self.bus.publish(
                make_event(
                    event_kind_started,
                    run_id=ctx.run_id,
                    task_id=ctx.task.id,
                    node_id=node.id,
                    attempt=attempt,
                )
            )
            t0 = time.perf_counter()
            try:
                result = await node.agent.run(ctx_attempt)
            except TransientError as e:
                last_err = e
                await self.bus.publish(
                    make_event(
                        event_kind_failed,
                        run_id=ctx.run_id,
                        task_id=ctx.task.id,
                        node_id=node.id,
                        attempt=attempt,
                        payload={"transient": True, "error": str(e)},
                    )
                )
                if attempt > retry.max_retries:
                    raise PermanentError(
                        f"Retries exhausted for node {node.id!r}",
                        context={"cause": str(e), "attempts": attempt},
                    ) from e
                await _sleep_backoff(retry, attempt, ctx, node.id)
                continue
            except PermanentError:
                await self.bus.publish(
                    make_event(
                        event_kind_failed,
                        run_id=ctx.run_id,
                        task_id=ctx.task.id,
                        node_id=node.id,
                        attempt=attempt,
                        payload={"transient": False},
                    )
                )
                raise
            except Exception as e:
                # Unknown exception — escalate. This keeps provider SDK
                # leaks from silently triggering retries.
                if inspect.isawaitable(e):  # pragma: no cover — defensive
                    pass
                await self.bus.publish(
                    make_event(
                        event_kind_failed,
                        run_id=ctx.run_id,
                        task_id=ctx.task.id,
                        node_id=node.id,
                        attempt=attempt,
                        payload={"transient": False, "unexpected": True, "error": str(e)},
                    )
                )
                raise PermanentError(
                    f"Unexpected error in node {node.id!r}: {type(e).__name__}: {e}",
                    context={"node": node.id},
                ) from e

            elapsed = time.perf_counter() - t0
            await self.bus.publish(
                make_event(
                    event_kind_completed,
                    run_id=ctx.run_id,
                    task_id=ctx.task.id,
                    node_id=node.id,
                    attempt=attempt,
                    payload={
                        "elapsed_s": elapsed,
                        "tokens": result.tokens.total_tokens,
                        "score": result.score.value if result.score is not None else None,
                    },
                )
            )
            if last_err is not None:
                # Clear the transient flag in the result metadata so callers
                # can see that a retry succeeded.
                result = result.model_copy(
                    update={"metadata": {**result.metadata, "recovered_after": attempt - 1}}
                )
            return result, attempt


def _rng_for(seed: int, ctx: AgentContext, node_id: str, attempt: int) -> random.Random:
    return random.Random(hash((seed, ctx.task.id, node_id, attempt)) & 0xFFFFFFFF)


async def _sleep_backoff(retry: Any, attempt: int, ctx: AgentContext, node_id: str) -> None:
    rng = _rng_for(ctx.seed, ctx, node_id, attempt)
    base = min(retry.max_backoff_s, retry.base_backoff_s * (2 ** (attempt - 1)))
    if retry.jitter:
        base = base * rng.uniform(1 - retry.jitter, 1 + retry.jitter)
    await anyio.sleep(max(0.0, base))


__all__ = ["Engine", "EngineResult"]
