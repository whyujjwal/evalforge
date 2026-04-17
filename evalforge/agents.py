"""Agent and Judge protocols + factory functions.

An ``Agent`` is a callable unit: given an :class:`AgentContext`, it produces
an :class:`AgentOutput`. ``Judge`` is the specialization that populates
``output.score``. Both satisfy the same runtime protocol; the engine
distinguishes them via ``kind``.

User-facing factories — :func:`llm_agent`, :func:`llm_judge`,
:func:`rule_judge` — return opaque instances that the pipeline composes.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from evalforge.errors import ConfigurationError, PermanentError
from evalforge.providers import get_provider
from evalforge.types import (
    AgentContext,
    AgentKind,
    AgentOutput,
    CompletionRequest,
    RetryPolicy,
    Score,
)

_ID_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_\-]*$")

_PLACEHOLDER = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")


@runtime_checkable
class Agent(Protocol):
    """The runtime contract every node must satisfy.

    Implementations are **async** and **deterministic modulo provider calls**:
    any non-provider randomness must derive from ``ctx.seed``.
    """

    id: str
    kind: AgentKind
    retry: RetryPolicy

    async def run(self, ctx: AgentContext) -> AgentOutput: ...


@runtime_checkable
class Judge(Agent, Protocol):
    """An :class:`Agent` that produces a :class:`Score`."""


def _validate_id(node_id: str) -> str:
    if not _ID_RE.match(node_id):
        raise ConfigurationError(
            f"Invalid node id {node_id!r}; must match {_ID_RE.pattern}",
            context={"id": node_id},
        )
    return node_id


def _render(template: str, namespace: dict[str, Any]) -> str:
    """Render a ``{placeholder}`` template against ``namespace``.

    Raises :class:`PermanentError` on missing keys. We use a narrow subset
    of ``str.format`` to avoid accidental interpretation of ``{{`` / ``}}``
    and to give a precise error message.
    """

    def repl(match: re.Match[str]) -> str:
        key = match.group(1)
        if key not in namespace:
            raise PermanentError(
                f"Prompt template referenced unknown key {key!r}",
                context={"available": sorted(namespace.keys())},
            )
        return str(namespace[key])

    return _PLACEHOLDER.sub(repl, template)


def _namespace_for(ctx: AgentContext) -> dict[str, Any]:
    """Build the template namespace from a context.

    Keys available in templates:
    - any key under ``task.input``
    - ``expected_output`` (from the task, ``""`` if unset)
    - ``task_id`` (alias of the task's id)
    - parent node ids → their ``value``
    """
    ns: dict[str, Any] = dict(ctx.task.input)
    ns.setdefault("expected_output", ctx.task.expected_output or "")
    ns.setdefault("task_id", ctx.task.id)
    for parent_id, value in ctx.parent_outputs.items():
        ns.setdefault(parent_id, value)
    return ns


# ---------- Concrete implementations (dataclasses, not Pydantic — these
# instances live in user code and are not serialized into storage). ----------


@dataclass(slots=True)
class _LlmAgent:
    id: str
    provider: str
    model: str
    prompt: str
    system: str | None = None
    temperature: float = 0.0
    max_tokens: int | None = None
    retry: RetryPolicy = field(default_factory=RetryPolicy)
    kind: AgentKind = AgentKind.AGENT

    async def run(self, ctx: AgentContext) -> AgentOutput:
        rendered = _render(self.prompt, _namespace_for(ctx))
        req = CompletionRequest(
            model=self.model,
            prompt=rendered,
            system=self.system,
            temperature=self.temperature,
            max_tokens=self.max_tokens,
        )
        resp = await get_provider(self.provider).complete(req)
        return AgentOutput(
            node_id=self.id,
            kind=AgentKind.AGENT,
            value=resp.text,
            tokens=resp.tokens,
            metadata={"model": resp.model, "provider": resp.provider},
        )


_SCORE_RE = re.compile(r"(?:score|rating)\s*[:=]\s*([0-9]*\.?[0-9]+)", re.IGNORECASE)


def _extract_score(text: str) -> float:
    """Best-effort parse of a judge's free-form verdict into a float.

    Matches ``score: 0.8`` / ``rating=1.0``. Falls back to the first float in
    the text. Returns ``0.0`` if nothing is parseable; judges that care about
    strict parsing should use ``rule_judge`` with an explicit lambda.
    """
    m = _SCORE_RE.search(text)
    if m:
        try:
            return float(m.group(1))
        except ValueError:  # pragma: no cover — regex guarantees float-ish
            pass
    for token in re.findall(r"-?\d+\.?\d*", text):
        try:
            return float(token)
        except ValueError:  # pragma: no cover
            continue
    return 0.0


@dataclass(slots=True)
class _LlmJudge:
    id: str
    provider: str
    model: str
    rubric: str
    parent: str | None = None
    system: str | None = None
    temperature: float = 0.0
    max_tokens: int | None = None
    retry: RetryPolicy = field(default_factory=RetryPolicy)
    kind: AgentKind = AgentKind.JUDGE

    async def run(self, ctx: AgentContext) -> AgentOutput:
        parent_value = _select_parent(ctx, self.parent)
        ns = _namespace_for(ctx)
        ns["output"] = parent_value
        ns.setdefault("parent_output", parent_value)
        rendered_rubric = _render(self.rubric, ns)
        full = (
            f"You are an evaluator. Score the output strictly.\n"
            f"Rubric: {rendered_rubric}\n"
            f"Task input: {ctx.task.input}\n"
            f"Output to judge: {parent_value}\n"
            f"Reply with 'score: <float between 0 and 1>' on the first line, "
            f"then a one-sentence rationale."
        )
        req = CompletionRequest(
            model=self.model,
            prompt=full,
            system=self.system,
            temperature=self.temperature,
            max_tokens=self.max_tokens,
        )
        resp = await get_provider(self.provider).complete(req)
        value = _extract_score(resp.text)
        score = Score(rubric_name=self.id, value=value, rationale=resp.text.strip())
        return AgentOutput(
            node_id=self.id,
            kind=AgentKind.JUDGE,
            value=resp.text,
            score=score,
            tokens=resp.tokens,
            metadata={"model": resp.model, "provider": resp.provider},
        )


def _select_parent(ctx: AgentContext, parent_hint: str | None) -> Any:
    """Pick which parent's value this judge is scoring.

    - If ``parent_hint`` is given, use that parent by id.
    - Else, if exactly one parent, use it.
    - Else, raise :class:`PermanentError` — ambiguity is a user bug.
    """
    if parent_hint is not None:
        if parent_hint not in ctx.parent_outputs:
            raise PermanentError(
                f"Judge {ctx.node_id!r} expected parent {parent_hint!r}",
                context={"parents": sorted(ctx.parent_outputs.keys())},
            )
        return ctx.parent_outputs[parent_hint]
    if len(ctx.parent_outputs) == 1:
        return next(iter(ctx.parent_outputs.values()))
    if not ctx.parent_outputs:
        raise PermanentError(
            f"Judge {ctx.node_id!r} has no parent output to score",
            context={"node_id": ctx.node_id},
        )
    raise PermanentError(
        f"Judge {ctx.node_id!r} has multiple parents; disambiguate with parent=...",
        context={"parents": sorted(ctx.parent_outputs.keys())},
    )


RuleFn = Callable[[Any], float | bool | Awaitable[float | bool]]


@dataclass(slots=True)
class _RuleJudge:
    id: str
    fn: RuleFn
    parent: str | None = None
    retry: RetryPolicy = field(default_factory=lambda: RetryPolicy(max_retries=0))
    kind: AgentKind = AgentKind.JUDGE

    async def run(self, ctx: AgentContext) -> AgentOutput:
        import inspect

        import anyio

        parent_value = _select_parent(ctx, self.parent)
        raw: Any
        if inspect.iscoroutinefunction(self.fn):
            raw = await self.fn(parent_value)
        else:
            # Offload sync user code to a worker thread. Never block the loop.
            raw = await anyio.to_thread.run_sync(self.fn, parent_value)
        value = float(raw) if not isinstance(raw, bool) else (1.0 if raw else 0.0)
        score = Score(
            rubric_name=self.id,
            value=value,
            rationale=f"rule_judge({self.id}) => {raw!r}",
        )
        return AgentOutput(
            node_id=self.id,
            kind=AgentKind.JUDGE,
            value=raw,
            score=score,
            metadata={"rule": True},
        )


# ---------- Public factory functions ----------


def llm_agent(
    id: str,
    *,
    model: str,
    prompt: str,
    provider: str = "anthropic",
    system: str | None = None,
    temperature: float = 0.0,
    max_tokens: int | None = None,
    retry: RetryPolicy | None = None,
) -> Agent:
    """Build an LLM-backed agent.

    Template placeholders in ``prompt`` are filled from the task input plus
    parent node outputs at run time.
    """
    return _LlmAgent(
        id=_validate_id(id),
        provider=provider,
        model=model,
        prompt=prompt,
        system=system,
        temperature=temperature,
        max_tokens=max_tokens,
        retry=retry or RetryPolicy(),
    )


def llm_judge(
    id: str,
    *,
    rubric: str,
    model: str = "mock",
    provider: str = "mock",
    parent: str | None = None,
    system: str | None = None,
    temperature: float = 0.0,
    max_tokens: int | None = None,
    retry: RetryPolicy | None = None,
) -> Judge:
    """Build an LLM-backed judge.

    ``rubric`` is a template that can reference ``{expected_output}``,
    ``{output}`` (the parent's value), and any key in ``task.input``.
    """
    return _LlmJudge(
        id=_validate_id(id),
        provider=provider,
        model=model,
        rubric=rubric,
        parent=parent,
        system=system,
        temperature=temperature,
        max_tokens=max_tokens,
        retry=retry or RetryPolicy(),
    )


def rule_judge(
    id: str,
    fn: RuleFn,
    *,
    parent: str | None = None,
) -> Judge:
    """Build a judge backed by a pure (possibly sync) callable.

    The callable receives the parent's ``value`` and returns either a bool or
    a float. Sync callables are offloaded to a threadpool — they never block
    the event loop.
    """
    return _RuleJudge(id=_validate_id(id), fn=fn, parent=parent)


__all__ = [
    "Agent",
    "Judge",
    "llm_agent",
    "llm_judge",
    "rule_judge",
]
