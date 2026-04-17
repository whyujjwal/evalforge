"""Pipeline declaration and DAG compilation.

The user declares a pipeline as a **nested list**: top-level entries are
sequential *stages*; a list within means the entries in that position run in
parallel and all share the previous stage's output as input.

Example::

    pipeline = [
        llm_agent("solver", ...),                       # stage 0
        [llm_judge("correctness", ...), rule_judge(...)],  # stage 1 (parallel)
    ]

Compilation walks this structure and produces a :class:`ResolvedDAG`:
a flat, validated set of nodes with parent/child edges and a topological
layering. :class:`ResolvedDAG` is pure data — no closures over user code
survive past compilation, which is what lets the engine snapshot it.

Rules enforced:

1. Every node ``id`` is unique across the whole pipeline.
2. The first stage must contain exactly one or more *agents* (not judges).
   Judges need a parent, and stage 0 has no parent.
3. Each judge must end up with exactly one parent, or must disambiguate
   with ``parent=...`` when its stage follows a fan-out.
4. The nested-list shape is a directed acyclic graph by construction (it's
   a sequence of bipartite layers), so there is no cycle check beyond the
   structural validation above.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Union, cast

from pydantic import BaseModel, ConfigDict, Field, field_validator

from evalforge.agents import Agent
from evalforge.errors import ConfigurationError
from evalforge.types import AgentKind, RunConfig, Task

PipelineStage = Union[Agent, Sequence[Agent]]
PipelineDecl = Sequence[PipelineStage]


class DAGNode(BaseModel):
    """A compiled node: id + role + edges. References the runtime ``agent``
    by attribute but is itself a plain data container."""

    model_config = ConfigDict(arbitrary_types_allowed=True, frozen=True)

    id: str
    kind: AgentKind
    agent: Agent
    parents: tuple[str, ...] = ()
    children: tuple[str, ...] = ()

    @property
    def is_judge(self) -> bool:
        return self.kind is AgentKind.JUDGE


class ResolvedDAG(BaseModel):
    """Compiled pipeline: flat node table plus topological layers.

    ``layers`` is the stage-major order produced by compilation; it is *also*
    a valid topological sort, so the engine can iterate it directly.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True, frozen=True)

    nodes: dict[str, DAGNode]
    layers: tuple[tuple[str, ...], ...]

    def roots(self) -> tuple[str, ...]:
        return tuple(n.id for n in self.nodes.values() if not n.parents)

    def leaves(self) -> tuple[str, ...]:
        return tuple(n.id for n in self.nodes.values() if not n.children)

    def judges(self) -> tuple[str, ...]:
        return tuple(n.id for n in self.nodes.values() if n.is_judge)

    def topo_order(self) -> tuple[str, ...]:
        return tuple(nid for layer in self.layers for nid in layer)

    def get(self, node_id: str) -> DAGNode:
        return self.nodes[node_id]


def _normalize_stage(stage: PipelineStage) -> tuple[Agent, ...]:
    if isinstance(stage, (list, tuple)):
        items: list[Agent] = list(stage)
        if not items:
            raise ConfigurationError("Empty parallel stage in pipeline")
        for n in items:
            if not hasattr(n, "id") or not hasattr(n, "run"):
                raise ConfigurationError(
                    "Pipeline stage contains a non-Agent value",
                    context={"value": repr(n)},
                )
        return tuple(items)
    single = cast(Agent, stage)
    if not hasattr(single, "id") or not hasattr(single, "run"):
        raise ConfigurationError("Pipeline stage is not an Agent", context={"value": repr(single)})
    return (single,)


def compile_pipeline(decl: PipelineDecl) -> ResolvedDAG:
    """Compile a declarative pipeline into a :class:`ResolvedDAG`.

    Raises :class:`ConfigurationError` on any structural problem.
    """
    if not decl:
        raise ConfigurationError("Pipeline is empty; at least one stage required")

    stages: list[tuple[Agent, ...]] = [_normalize_stage(s) for s in decl]

    # Enforce uniqueness of node ids.
    seen: set[str] = set()
    for stage in stages:
        for agent in stage:
            if agent.id in seen:
                raise ConfigurationError(
                    f"Duplicate node id in pipeline: {agent.id!r}",
                    context={"id": agent.id},
                )
            seen.add(agent.id)

    # Stage 0 must contain at least one agent (not judge-only).
    if all(a.kind is AgentKind.JUDGE for a in stages[0]):
        raise ConfigurationError(
            "First pipeline stage contains only judges; judges require a parent.",
            context={"stage0_ids": [a.id for a in stages[0]]},
        )

    # Build parents/children edges: every node in stage i is a child of every
    # node in stage i-1. This mirrors the nested-list semantics.
    parents_of: dict[str, list[str]] = {}
    children_of: dict[str, list[str]] = {}
    for i, stage in enumerate(stages):
        for agent in stage:
            parents_of.setdefault(agent.id, [])
            children_of.setdefault(agent.id, [])
        if i == 0:
            continue
        prev_ids = [a.id for a in stages[i - 1]]
        for agent in stage:
            parents_of[agent.id].extend(prev_ids)
            for p in prev_ids:
                children_of[p].append(agent.id)

    # Judges in a fan-in position must pick a parent. llm_judge/rule_judge
    # carry an optional ``parent`` attribute we can inspect via duck typing.
    for stage in stages:
        for agent in stage:
            if agent.kind is AgentKind.JUDGE:
                ps = parents_of[agent.id]
                parent_hint = getattr(agent, "parent", None)
                if len(ps) > 1 and parent_hint is None:
                    raise ConfigurationError(
                        f"Judge {agent.id!r} has {len(ps)} parents; "
                        f"pass parent=<id> to disambiguate.",
                        context={"judge": agent.id, "parents": ps},
                    )
                if parent_hint is not None and parent_hint not in ps:
                    raise ConfigurationError(
                        f"Judge {agent.id!r} names parent {parent_hint!r} "
                        "which is not actually a parent in the pipeline.",
                        context={"judge": agent.id, "claimed": parent_hint, "actual": ps},
                    )

    nodes: dict[str, DAGNode] = {}
    for stage in stages:
        for agent in stage:
            nodes[agent.id] = DAGNode(
                id=agent.id,
                kind=agent.kind,
                agent=agent,
                parents=tuple(parents_of[agent.id]),
                children=tuple(children_of[agent.id]),
            )

    layers = tuple(tuple(a.id for a in stage) for stage in stages)
    return ResolvedDAG(nodes=nodes, layers=layers)


class Suite(BaseModel):
    """A named collection of tasks paired with a pipeline declaration.

    The declaration is compiled eagerly on construction so configuration
    errors surface at definition time, not mid-run.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True, frozen=True)

    name: str = Field(min_length=1, max_length=256)
    tasks: tuple[Task, ...]
    pipeline: tuple[tuple[Agent, ...], ...]
    config: RunConfig = Field(default_factory=RunConfig)

    @field_validator("tasks")
    @classmethod
    def _tasks_non_empty_unique(cls, v: tuple[Task, ...]) -> tuple[Task, ...]:
        if not v:
            raise ValueError("Suite must have at least one task")
        ids = [t.id for t in v]
        if len(set(ids)) != len(ids):
            dupes = sorted({i for i in ids if ids.count(i) > 1})
            raise ValueError(f"Duplicate task ids: {dupes}")
        return v

    def __init__(self, **data: object) -> None:
        pipeline = data.get("pipeline")
        if pipeline is not None and not isinstance(pipeline, tuple):
            # Accept the user-facing list-of-list form and normalize.
            if not isinstance(pipeline, (list, tuple)):
                raise ConfigurationError("Suite.pipeline must be a list")
            compiled = compile_pipeline(pipeline)  # validates early
            data["pipeline"] = tuple(
                tuple(compiled.get(nid).agent for nid in layer) for layer in compiled.layers
            )
            data["_resolved_dag"] = compiled
        tasks = data.get("tasks")
        if isinstance(tasks, list):
            data["tasks"] = tuple(tasks)
        super().__init__(**{k: v for k, v in data.items() if k != "_resolved_dag"})
        object.__setattr__(self, "_resolved_dag", data.get("_resolved_dag"))

    @property
    def dag(self) -> ResolvedDAG:
        """Return the compiled DAG, computing it on demand if needed."""
        cached = getattr(self, "_resolved_dag", None)
        if cached is None:
            dag = compile_pipeline(list(_as_decl(self.pipeline)))
            object.__setattr__(self, "_resolved_dag", dag)
            return dag
        return cached  # type: ignore[no-any-return]

    @property
    def task_ids(self) -> tuple[str, ...]:
        return tuple(t.id for t in self.tasks)


def _as_decl(pipeline: Iterable[Iterable[Agent]]) -> list[PipelineStage]:
    decl: list[PipelineStage] = []
    for stage in pipeline:
        stage_t = tuple(stage)
        decl.append(stage_t[0] if len(stage_t) == 1 else list(stage_t))
    return decl


__all__ = [
    "DAGNode",
    "PipelineDecl",
    "PipelineStage",
    "ResolvedDAG",
    "Suite",
    "compile_pipeline",
]
