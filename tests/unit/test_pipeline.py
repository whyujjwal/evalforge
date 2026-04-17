"""Pipeline compilation, DAG resolution, and Suite validation."""

from __future__ import annotations

import pytest
from evalforge import (
    ConfigurationError,
    Suite,
    Task,
    compile_pipeline,
    llm_agent,
    llm_judge,
    rule_judge,
)
from evalforge.types import AgentKind


def _solver(id_: str = "solver") -> object:
    return llm_agent(id_, provider="mock", model="mock", prompt="Solve: {question}")


def _judge(id_: str = "correctness") -> object:
    return llm_judge(
        id_,
        provider="mock",
        model="mock",
        rubric="Is {output} equal to {expected_output}?",
    )


def test_compile_linear_pipeline() -> None:
    dag = compile_pipeline([_solver(), _judge()])
    assert dag.topo_order() == ("solver", "correctness")
    assert dag.get("correctness").parents == ("solver",)
    assert dag.get("solver").children == ("correctness",)


def test_compile_fanout_pipeline() -> None:
    dag = compile_pipeline(
        [
            _solver(),
            [
                _judge("correctness"),
                rule_judge("format", lambda out: str(out).strip().isdigit()),
            ],
        ]
    )
    assert dag.layers == (("solver",), ("correctness", "format"))
    assert set(dag.get("solver").children) == {"correctness", "format"}
    assert dag.get("correctness").parents == ("solver",)
    assert dag.get("format").parents == ("solver",)


def test_fan_in_requires_parent_hint_for_judge() -> None:
    # Two agents fanning into one judge — ambiguous unless disambiguated.
    with pytest.raises(ConfigurationError):
        compile_pipeline(
            [
                [_solver("a1"), _solver("a2")],
                _judge("j1"),
            ]
        )


def test_fan_in_parent_hint_must_be_an_actual_parent() -> None:
    with pytest.raises(ConfigurationError):
        compile_pipeline(
            [
                [_solver("a1"), _solver("a2")],
                llm_judge(
                    "j1",
                    provider="mock",
                    model="mock",
                    rubric="{output}",
                    parent="ghost",
                ),
            ]
        )


def test_fan_in_with_valid_parent_hint() -> None:
    dag = compile_pipeline(
        [
            [_solver("a1"), _solver("a2")],
            llm_judge(
                "j1",
                provider="mock",
                model="mock",
                rubric="{output}",
                parent="a1",
            ),
        ]
    )
    assert dag.get("j1").kind is AgentKind.JUDGE


def test_duplicate_ids_rejected() -> None:
    with pytest.raises(ConfigurationError):
        compile_pipeline([_solver("x"), _judge("x")])


def test_empty_pipeline_rejected() -> None:
    with pytest.raises(ConfigurationError):
        compile_pipeline([])


def test_empty_parallel_stage_rejected() -> None:
    with pytest.raises(ConfigurationError):
        compile_pipeline([_solver(), []])


def test_stage_zero_cannot_be_judge_only() -> None:
    with pytest.raises(ConfigurationError):
        compile_pipeline([_judge("lonely")])


def test_invalid_node_id_rejected() -> None:
    with pytest.raises(ConfigurationError):
        llm_agent("has space", provider="mock", model="mock", prompt="...")


def test_non_agent_in_pipeline_rejected() -> None:
    with pytest.raises(ConfigurationError):
        compile_pipeline([_solver(), "not-an-agent"])  # type: ignore[list-item]


def test_suite_compiles_eagerly() -> None:
    suite = Suite(
        name="math",
        tasks=[Task(id="t1", input={"question": "1+1"}, expected_output="2")],
        pipeline=[_solver(), _judge()],
    )
    assert suite.dag.topo_order() == ("solver", "correctness")
    assert suite.task_ids == ("t1",)


def test_suite_duplicate_task_ids_rejected() -> None:
    with pytest.raises(Exception):
        Suite(
            name="x",
            tasks=[Task(id="a", input={}), Task(id="a", input={})],
            pipeline=[_solver(), _judge()],
        )


def test_suite_requires_at_least_one_task() -> None:
    with pytest.raises(Exception):
        Suite(name="x", tasks=[], pipeline=[_solver(), _judge()])


def test_resolved_dag_roots_and_leaves() -> None:
    dag = compile_pipeline(
        [
            _solver("s"),
            [_judge("j1"), rule_judge("j2", lambda _v: True)],
        ]
    )
    assert dag.roots() == ("s",)
    assert set(dag.leaves()) == {"j1", "j2"}
    assert set(dag.judges()) == {"j1", "j2"}
