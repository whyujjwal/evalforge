# evalforge

A minimal, composable **multi-agent evaluation harness** for LLM pipelines.
Think "pytest of LLM evals": declarative suites, a DAG of agents and judges,
deterministic aggregation, and clean resumability.

> Status: **early development.** The public API under
> `from evalforge import ...` is what's stable; everything under
> `evalforge.<module>` is private and may change.

## Install

```bash
uv sync --all-extras
```

## Quick taste

```python
from evalforge import Suite, Task, llm_agent, llm_judge, rule_judge

suite = Suite(
    name="math_word_problems",
    tasks=[Task(id="t1", input={"question": "..."}, expected_output="42")],
    pipeline=[
        llm_agent("solver", model="claude-sonnet-4-5", prompt="Solve: {question}"),
        [
            llm_judge("correctness", rubric="Is the answer correct given expected={expected_output}?"),
            rule_judge("format", lambda out: out.strip().isdigit()),
        ],
    ],
)
```

Run it:

```bash
evalforge run suite.py
evalforge show <run_id>
evalforge diff <run_a> <run_b>
```

## Architecture

The dependency direction (enforced by `import-linter`):

```
types / errors  →  events  →  providers  →  agents  →  pipeline  →  storage  →  engine  →  cli / server
```

The event bus is the single source of truth for observability — storage,
CLI progress, and metrics are all subscribers.

## Development

```bash
uv sync --all-extras
uv run pytest
uv run ruff check evalforge tests
uv run mypy evalforge
uv run lint-imports
```

See `SESSION_LOG.md` for the design narrative of how this was built.
