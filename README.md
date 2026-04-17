# evalforge

A minimal, composable **multi-agent evaluation harness** for LLM pipelines.
Think "pytest of LLM evals": declarative suites, a DAG of agents and judges,
deterministic aggregation, and clean resumability.

> Status: **v0.1.0 (early development).** Public API under
> `from evalforge import ...` is stable within 0.x; everything under
> `evalforge.<module>` is private and may change.

---

## Quick start

```bash
uv sync --all-extras
uv run evalforge run examples/math_suite.py
```

You should see JSON log lines on stderr and a `run_<id>` printed on stdout.

```bash
uv run evalforge list               # all recent runs
uv run evalforge show <run_id>      # full run JSON
uv run evalforge diff <a> <b>       # per-task score deltas between two runs
```

---

## Declaring a suite

```python
from evalforge import Suite, Task, llm_agent, llm_judge, rule_judge

suite = Suite(
    name="math_word_problems",
    tasks=[
        Task(id="t1", input={"question": "What is 6 * 7?"}, expected_output="42"),
    ],
    pipeline=[
        llm_agent(
            "solver",
            provider="anthropic",
            model="claude-sonnet-4-5",
            prompt="Solve: {question}",
            max_tokens=256,
        ),
        [
            llm_judge(
                "correctness",
                provider="anthropic",
                model="claude-sonnet-4-5",
                rubric="Is {output} equal to {expected_output}?",
                max_tokens=64,
            ),
            rule_judge("is_digit", lambda out: out.strip().isdigit()),
        ],
    ],
)
```

The top-level `pipeline` is a sequence of **stages**. A nested list means
"these run in parallel against the previous stage's output." Fan-in that
ambiguates which parent a judge scores must declare `parent="<id>"`.

---

## Architecture

Module dependency order (enforced by `import-linter` — PRs that violate it
fail CI):

```
types / errors  →  events  →  providers  →  agents  →  pipeline  →  storage  →  engine  →  cli / server
```

**The event bus is the single source of truth for observability.** Every
state transition the engine makes is a published `Event`. Storage, CLI
progress, and metrics are all subscribers — none of them reach into engine
internals. This is what makes resume clean: persisted state is
reconstructible from the event stream alone.

Key properties:

- **Frozen domain types.** `Task`, `Score`, `TaskResult`, `Run`, etc. are
  immutable Pydantic v2 models. Events you see at emit time are the same
  bytes you read back.
- **Async end-to-end.** The engine is `anyio`-based with a task-level
  semaphore for bounded concurrency; fan-out within a DAG stage uses a
  `TaskGroup`.
- **Retries at node granularity, not task.** A rate-limited judge retries
  without re-running the solver. Jitter is seeded on
  `(run.seed, task_id, node_id, attempt)` — deterministic, uncorrelated.
- **Provider boundary is narrow.** `AnthropicProvider` and `OpenAIProvider`
  are built directly on `httpx` (no vendor SDK). HTTP `429`/`5xx`/network
  errors become `ProviderTransientError`; `4xx` becomes
  `ProviderPermanentError`. User code never sees a raw `httpx` exception.
- **Secrets are redacted in logs.** `structlog` pipeline scrubs any
  `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` substring before encoding, even
  inside nested dicts and lists.

---

## Resumability

```bash
# First run; imagine the process gets killed mid-way
uv run evalforge run suite.py

# List runs to find the partial one
uv run evalforge list

# Resume it
uv run evalforge run suite.py --resume <run_id>
```

Invariants verified by a fault-injection test (`tests/integration/test_storage.py`):
1. No task is scored twice.
2. No task is silently dropped.
3. A resumed run's results equal those of a clean run on the same seed.

---

## Error taxonomy

| Class                      | Meaning                                    | Engine behavior         |
|----------------------------|--------------------------------------------|-------------------------|
| `TransientError`           | Retryable (network, rate limit, 5xx)       | Retry with backoff      |
| `PermanentError`           | Not retryable (4xx, bad schema, bad input) | Task marked `failed`, run continues |
| `FatalError`               | Invariant violation                        | Aborts the run          |
| `ConfigurationError`       | Bad `Suite` / `Agent` wiring               | Raised at declaration   |
| `ProviderTransient/Permanent` | Specialization per provider             | As above                |

---

## API surface

```python
from evalforge import (
    Suite, Task, Score, TaskResult, Run, RunConfig, RetryPolicy,
    Agent, Judge,
    llm_agent, llm_judge, rule_judge,
    compile_pipeline,
    Engine, EngineResult, EventBus, Event,
    Storage, SQLiteStorage,
    # Errors
    EvalforgeError, TransientError, PermanentError, FatalError,
    ConfigurationError, ProviderError, ProviderTransientError, ProviderPermanentError,
)
```

Anything not re-exported from `evalforge/__init__.py` is private.

---

## Server

```bash
uv run uvicorn evalforge.server:app
# POST /runs         { "suite_path": "/abs/path/suite.py" }
# GET  /runs
# GET  /runs/{id}
# GET  /runs/{id}/events   (SSE)
```

The FastAPI app is a thin adapter over the library — same contract as the
CLI, over HTTP.

---

## Development

```bash
uv sync --all-extras
uv run pytest                                # 55 tests, ~2 seconds
uv run ruff check evalforge tests
uv run ruff format --check evalforge tests
uv run mypy evalforge                        # strict, 0 ignores
uv run lint-imports                          # layered architecture contract
uv run python scripts/check_core_coverage.py # 90% floor on core
```

CI runs the exact same sequence on Python 3.11 and 3.12.

See [`SESSION_LOG.md`](SESSION_LOG.md) for the design narrative of how this
library was built — the decisions, the dead ends, the recoveries.
