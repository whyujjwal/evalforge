"""Domain type invariants: frozen, forbid-extra, validation."""

from __future__ import annotations

import pytest
from evalforge import (
    AgentKind,
    AgentOutput,
    RetryPolicy,
    Run,
    RunStatus,
    Score,
    Task,
    TaskResult,
    TaskStatus,
    TokenUsage,
)
from evalforge.types import _now
from pydantic import ValidationError


def test_task_is_frozen() -> None:
    t = Task(id="a", input={"q": "?"})
    with pytest.raises(ValidationError):
        t.id = "b"  # type: ignore[misc]


def test_task_rejects_extra_fields() -> None:
    with pytest.raises(ValidationError):
        Task(id="a", input={}, bogus=1)  # type: ignore[call-arg]


def test_task_id_must_not_have_whitespace() -> None:
    with pytest.raises(ValidationError):
        Task(id="bad id", input={})


def test_task_copy_update_produces_new_value() -> None:
    t = Task(id="a", input={"q": "?"})
    t2 = t.model_copy(update={"input": {"q": "!"}})
    assert t.input == {"q": "?"}
    assert t2.input == {"q": "!"}
    assert t is not t2


def test_token_usage_additive() -> None:
    a = TokenUsage(input_tokens=10, output_tokens=5, cost_usd=0.01)
    b = TokenUsage(input_tokens=3, output_tokens=7, cost_usd=0.02)
    c = a + b
    assert c.input_tokens == 13
    assert c.output_tokens == 12
    assert c.total_tokens == 25
    assert abs(c.cost_usd - 0.03) < 1e-9


def test_retry_policy_defaults_and_bounds() -> None:
    rp = RetryPolicy()
    assert rp.max_retries == 3
    with pytest.raises(ValidationError):
        RetryPolicy(max_retries=-1)
    with pytest.raises(ValidationError):
        RetryPolicy(jitter=2.0)


def test_run_aggregates() -> None:
    now = _now()
    results = (
        TaskResult(
            task_id="t1",
            status=TaskStatus.OK,
            scores=(Score(rubric_name="correctness", value=1.0),),
            tokens=TokenUsage(input_tokens=5, output_tokens=5),
            started_at=now,
            finished_at=now,
        ),
        TaskResult(
            task_id="t2",
            status=TaskStatus.OK,
            scores=(
                Score(rubric_name="correctness", value=0.0),
                Score(rubric_name="style", value=0.5),
            ),
            tokens=TokenUsage(input_tokens=2, output_tokens=3),
            started_at=now,
            finished_at=now,
        ),
    )
    run = Run(suite_name="s", status=RunStatus.COMPLETED, results=results)
    assert run.total_tokens.total_tokens == 15
    assert run.mean_score_per_rubric["correctness"] == pytest.approx(0.5)
    assert run.mean_score_per_rubric["style"] == pytest.approx(0.5)
    assert run.completed_task_ids == {"t1", "t2"}


def test_agent_output_kinds() -> None:
    out = AgentOutput(node_id="n", kind=AgentKind.AGENT, value="hello")
    assert out.score is None
    judge_out = AgentOutput(
        node_id="j",
        kind=AgentKind.JUDGE,
        value="score: 0.8 good",
        score=Score(rubric_name="j", value=0.8),
    )
    assert judge_out.score is not None
