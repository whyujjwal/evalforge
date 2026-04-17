"""Domain types — the shape of the world.

All models are **frozen** Pydantic v2 models with ``extra="forbid"``. Mutation
happens via :meth:`pydantic.BaseModel.model_copy(update=...)`. Freezing is
what makes the event stream and storage layer trustworthy: a value captured
in an event on emit is guaranteed to equal the value read back later.

This module is a **leaf** in the dependency graph: it imports nothing from
the rest of the package. No I/O, no behavior, no logging.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator


def _now() -> datetime:
    """UTC now with tz info — we never store naive datetimes."""
    return datetime.now(UTC)


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex[:16]}"


class _Frozen(BaseModel):
    """Shared base: frozen, forbid extra, validate on assign."""

    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        validate_assignment=True,
        str_strip_whitespace=False,
    )


# ---------- Core domain ----------


class Task(_Frozen):
    """A single input to evaluate.

    ``id`` must be unique within a Suite; ``input`` is an arbitrary
    JSON-serializable dict that agents interpolate into prompts.
    """

    id: str = Field(min_length=1, max_length=256)
    input: dict[str, Any] = Field(default_factory=dict)
    expected_output: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("id")
    @classmethod
    def _id_no_whitespace(cls, v: str) -> str:
        if v != v.strip() or any(c.isspace() for c in v):
            raise ValueError("Task.id must not contain whitespace")
        return v


class Score(_Frozen):
    """A structured verdict from a judge.

    ``value`` is conventionally in ``[0, 1]`` but we do not enforce that — a
    judge may legitimately emit negative or out-of-range signals. Consumers
    that aggregate should clamp or document their normalization.
    """

    rubric_name: str = Field(min_length=1, max_length=128)
    value: float
    rationale: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)


class TokenUsage(_Frozen):
    """Input + output tokens for a single model call."""

    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    cost_usd: float = Field(default=0.0, ge=0.0)

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def __add__(self, other: TokenUsage) -> TokenUsage:
        if not isinstance(other, TokenUsage):  # pragma: no cover
            return NotImplemented
        return TokenUsage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cost_usd=self.cost_usd + other.cost_usd,
        )


class CompletionRequest(_Frozen):
    """A provider-agnostic completion request.

    Providers translate this into their native wire format at the boundary.
    Deliberately narrow: this library is not a general-purpose SDK.
    """

    model: str
    prompt: str
    temperature: float = 0.0
    max_tokens: int | None = None
    system: str | None = None
    stop: tuple[str, ...] = ()
    metadata: dict[str, Any] = Field(default_factory=dict)


class CompletionResponse(_Frozen):
    """The provider-agnostic response."""

    text: str
    tokens: TokenUsage = Field(default_factory=TokenUsage)
    model: str
    provider: str
    raw: dict[str, Any] = Field(default_factory=dict)


class RetryPolicy(_Frozen):
    """Per-agent retry policy applied only to :class:`TransientError`.

    Backoff is exponential with jitter: ``min(max_backoff, base * 2**attempt)``
    scaled by ``uniform(1 - jitter, 1 + jitter)``. The engine draws jitter
    from a seeded RNG so tests are deterministic.
    """

    max_retries: int = Field(default=3, ge=0, le=20)
    base_backoff_s: float = Field(default=0.25, ge=0.0, le=60.0)
    max_backoff_s: float = Field(default=30.0, ge=0.0, le=600.0)
    jitter: float = Field(default=0.25, ge=0.0, le=1.0)


class AgentKind(StrEnum):
    AGENT = "agent"
    JUDGE = "judge"


class AgentContext(_Frozen):
    """What an agent sees when invoked.

    ``parent_outputs`` maps parent node id → that parent's ``value`` (the
    string or structured output of the parent's run). This is how a judge at
    stage 2 sees the solver's answer from stage 1.
    """

    run_id: str
    task: Task
    node_id: str
    parent_outputs: dict[str, Any] = Field(default_factory=dict)
    seed: int = 0
    attempt: int = 0


class AgentOutput(_Frozen):
    """Structured result of one node's invocation.

    - For an **agent**: ``value`` is the agent's output (typically a string),
      and ``score`` is ``None``.
    - For a **judge**: ``value`` is the raw verdict text (for audit) and
      ``score`` is the structured verdict downstream code consumes.
    """

    node_id: str
    kind: AgentKind
    value: Any = None
    score: Score | None = None
    tokens: TokenUsage = Field(default_factory=TokenUsage)
    metadata: dict[str, Any] = Field(default_factory=dict)


class TaskStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    OK = "ok"
    FAILED = "failed"


class RunStatus(StrEnum):
    CREATED = "created"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class TaskResult(_Frozen):
    """Aggregate result for one task in one run.

    A ``TaskResult`` is the unit of persistence: storage writes it along with
    all its scores in a single transaction. Partial writes are impossible by
    construction.
    """

    task_id: str
    status: TaskStatus
    outputs: dict[str, Any] = Field(default_factory=dict)
    scores: tuple[Score, ...] = ()
    tokens: TokenUsage = Field(default_factory=TokenUsage)
    error: str | None = None
    started_at: datetime
    finished_at: datetime
    attempts: int = Field(default=1, ge=1)


class RunConfig(_Frozen):
    """Engine-level knobs for a run.

    ``concurrency`` is a *task-level* semaphore. Parallel fan-out within one
    task's DAG stage is unbounded (and bounded in practice by stage width).
    """

    concurrency: int = Field(default=8, ge=1, le=1024)
    seed: int = 0
    shutdown_grace_s: float = Field(default=30.0, ge=0.0, le=3600.0)
    retry: RetryPolicy = Field(default_factory=RetryPolicy)


class Run(_Frozen):
    """Immutable snapshot of an execution.

    The engine emits a new ``Run`` each time state changes. Storage persists
    these snapshots append-only by ``id``.
    """

    id: str = Field(default_factory=lambda: _new_id("run"))
    suite_name: str
    status: RunStatus = RunStatus.CREATED
    config: RunConfig = Field(default_factory=RunConfig)
    created_at: datetime = Field(default_factory=_now)
    started_at: datetime | None = None
    finished_at: datetime | None = None
    results: tuple[TaskResult, ...] = ()
    error: str | None = None

    @property
    def total_tokens(self) -> TokenUsage:
        acc = TokenUsage()
        for r in self.results:
            acc = acc + r.tokens
        return acc

    @property
    def completed_task_ids(self) -> frozenset[str]:
        return frozenset(r.task_id for r in self.results if r.status is TaskStatus.OK)

    @property
    def mean_score_per_rubric(self) -> dict[str, float]:
        buckets: dict[str, list[float]] = {}
        for r in self.results:
            for s in r.scores:
                buckets.setdefault(s.rubric_name, []).append(s.value)
        return {k: sum(v) / len(v) for k, v in buckets.items() if v}


# ---------- Events ----------


EventKind = Literal[
    "run_started",
    "run_finished",
    "task_started",
    "task_finished",
    "agent_started",
    "agent_completed",
    "agent_failed",
    "judge_started",
    "judge_completed",
    "judge_failed",
    "engine_error",
]


class Event(_Frozen):
    """A single point on the event stream.

    Storage, CLI progress, and metrics are derived solely from this stream.
    Events are assigned a monotonic ``seq`` by the bus on publish.
    """

    seq: int = 0
    kind: EventKind
    ts: datetime = Field(default_factory=_now)
    run_id: str
    task_id: str | None = None
    node_id: str | None = None
    attempt: int = 0
    payload: dict[str, Any] = Field(default_factory=dict)


__all__ = [
    "AgentContext",
    "AgentKind",
    "AgentOutput",
    "CompletionRequest",
    "CompletionResponse",
    "Event",
    "EventKind",
    "RetryPolicy",
    "Run",
    "RunConfig",
    "RunStatus",
    "Score",
    "Task",
    "TaskResult",
    "TaskStatus",
    "TokenUsage",
]
