# Session Log — evalforge

A Q&A-style record of the decisions behind this library. Every section is a question a skeptical reviewer might ask, answered with the reasoning at the time — what we considered, what we chose, and what the alternative would have cost us.

The structure here is deliberate: the code is the *what*; this log is the *why*. A senior engineer reading the diff can infer the design in an hour. What they cannot infer is which alternatives were weighed and discarded, or which constraints were load-bearing versus aesthetic. That's what this document preserves.

---

## Table of contents

- [Framing the problem](#framing-the-problem)
- [Q1: What problem is evalforge actually solving?](#q1-what-problem-is-evalforge-actually-solving)
- [Q2: Why a library, not a service?](#q2-why-a-library-not-a-service)
- [Q3: Why build the tooling before the code?](#q3-why-build-the-tooling-before-the-code)
- [Q4: Why frozen Pydantic models for the domain?](#q4-why-frozen-pydantic-models-for-the-domain)
- [Q5: Three error categories. Why not two, or five?](#q5-three-error-categories-why-not-two-or-five)
- [Q6: Why this pipeline DSL?](#q6-why-this-pipeline-dsl)
- [Q7: Why compile the DAG eagerly at `Suite` construction?](#q7-why-compile-the-dag-eagerly-at-suite-construction)
- [Q8: How did we decide the event bus semantics?](#q8-how-did-we-decide-the-event-bus-semantics)
- [Q9: Why is storage a subscriber and not a direct dependency?](#q9-why-is-storage-a-subscriber-and-not-a-direct-dependency)
- [Q10: What made us confident resume is correct?](#q10-what-made-us-confident-resume-is-correct)
- [Q11: Why task-level concurrency rather than node-level?](#q11-why-task-level-concurrency-rather-than-node-level)
- [Q12: Why retry at the node, not the task?](#q12-why-retry-at-the-node-not-the-task)
- [Q13: Why no vendor SDKs for Anthropic / OpenAI?](#q13-why-no-vendor-sdks-for-anthropic--openai)
- [Q14: Why is secret redaction a correctness test, not a code review concern?](#q14-why-is-secret-redaction-a-correctness-test-not-a-code-review-concern)
- [Q15: How did we keep the layered architecture honest?](#q15-how-did-we-keep-the-layered-architecture-honest)
- [Q16: Where did we hit real friction, and how did we climb out?](#q16-where-did-we-hit-real-friction-and-how-did-we-climb-out)
- [Q17: What did we deliberately not build?](#q17-what-did-we-deliberately-not-build)
- [Q18: If someone forked this tomorrow, where would they struggle?](#q18-if-someone-forked-this-tomorrow-where-would-they-struggle)
- [Milestone recap](#milestone-recap)
- [Final state](#final-state)

---

## Framing the problem

Before any code, we spent time holding the problem in view. Not just "build a spec" — but *what is the real complexity here, and where does it live?*

The surface problem: run a dataset of inputs through one-or-more LLM agents, score the outputs along multiple axes, persist the results, make it resumable, make it observable.

The deeper complexity is not any single piece. It's the **interactions**:

- The execution model has to be async (LLM calls are network-bound) *and* deterministic-in-test (no one wants flaky eval harnesses).
- Persistence has to be transactional (partial writes corrupt score aggregates) *and* decoupled from the engine (so you can swap SQLite for Postgres without touching the hot path).
- Observability has to be complete (every state transition) *and* cheap (the event stream runs in the critical path of every task).
- The declarative API has to be simple enough that a user writes a suite in one screen *and* expressive enough to encode real DAGs with fan-out and fan-in.
- The retry logic has to be robust (transient network failures shouldn't sink a run) *and* bounded (runaway retries are how you burn a month's API budget in an afternoon).

Any one of these is tractable. The engineering task is making them compose without leakage between layers.

That framing is what drives nearly every decision below.

---

## Q1: What problem is evalforge actually solving?

**The problem is orchestration, not scoring.**

Other tools — Braintrust, Langsmith, Inspect — have rubrics, judge implementations, datasets, UIs. We're not competing on any of that. The question we kept asking ourselves was: *what's the minimal substrate you could embed inside any of those products?*

The answer is a thing that:
- takes a declarative description of (inputs × pipeline),
- executes it with bounded concurrency,
- writes results transactionally,
- emits a complete event stream,
- resumes cleanly after a crash.

That's the contract. Everything else — specific judges, specific models, specific UIs — sits above this and plugs in through narrow protocols (`Agent`, `Judge`, `Provider`, `Storage`).

**Why that framing?** Because the failure mode of evaluation tools today is not "not enough rubrics." It's "I can't trust the numbers because the harness crashed at task 847, I restarted, and now I don't know if task 500 was scored against the old prompt or the new one." We wanted to be the library the other libraries don't have to write.

---

## Q2: Why a library, not a service?

The spec says "library-first," but we had to decide what that actually means in code. We ended up with a hard rule:

> If you're writing business logic in `cli.py` or `server.py`, it belongs in the core.

Every CLI command and every HTTP handler is five to fifteen lines. They do two things: marshal inputs, call a library function, marshal outputs. Nothing else.

**Why push so hard on this?** Two reasons.

First, the CLI and server are *examples of integration*, not the library. If you're a vendor who wants to embed eval orchestration into your own product, you shouldn't have to copy-paste from `cli.py` to reconstruct the logic. The library itself has to be the thing.

Second, thin adapters are testable in ways thick ones aren't. The `POST /runs` handler is trivially correct because it delegates to `load_suite` → `Engine.run` → `store.attach`. We don't need end-to-end integration tests for the server to catch bugs; unit tests on each of those library calls cover the behavior.

**What the alternative would have cost:** the CLI would have evolved argument-parsing quirks the library didn't know about. The server would have grown its own state management. Six months later, someone trying to embed the library would find a dozen subtly different ways of running a suite and would choose the wrong one.

---

## Q3: Why build the tooling before the code?

Before a single domain type was written, we had `pyproject.toml`, ruff, `mypy --strict`, `import-linter`, pre-commit, and CI configured and green on an empty package.

**The question this answers is: what's the cost of being wrong?**

If you write `types.py` first and then set up mypy later, you're going to find type errors. Some will be easy. Some will force you to redesign an interface. If you don't find them until after you've written `engine.py`, the redesign ripples through everything downstream. The work you already did is on shaky ground and you don't know it yet.

The same logic applies to the import-linter architecture contract. Writing the contract *first* means the first import you ever write is checked. You never build up a mountain of violations that you have to choose between fixing or suppressing.

**The concrete payoff in this session:** when we wrote `types.py`, the import-linter contract already forbade it from importing anything else in the project. It could not accidentally reach for `httpx` or `sqlalchemy`, even as a convenience. The clean layer wasn't something we maintained — it was something the tooling made impossible to violate.

---

## Q4: Why frozen Pydantic models for the domain?

The spec mandated this, but it's worth articulating why the mandate is correct. It's load-bearing.

**Mutation breaks the event stream.** If `TaskResult` is mutable and the engine publishes a `task_finished` event containing `result`, a subscriber reading that event ten milliseconds later might see a different object than what was published. With frozen models, the event is a snapshot by construction. You can ship it over SSE, stuff it in JSON, read it back a week later — it's still the same bytes.

**Mutation breaks storage.** `save_task_result(run_id, result)` serializes the result into SQLite. If the caller mutates `result` after the call, the in-memory copy and the on-disk copy diverge silently. Frozen models plus Pydantic's `extra="forbid"` mean: every `TaskResult` that reaches storage is exactly the `TaskResult` the engine produced.

**Mutation breaks testing.** `result.model_copy(update={...})` is syntactically noisier than `result.status = "ok"`. It's also the reason we can write:

```python
assert resumed_by_id[tid].scores == clean_by_id[tid].scores
```

and trust it. If scores were a mutable list and a test elsewhere appended to it, this comparison would be a time-traveling hazard. Frozen containers + tuples make the test a real assertion instead of a hopeful one.

**What it costs:** a little verbosity (`.model_copy(update=...)` instead of attribute assignment) and the need for `object.__setattr__` in the one place `Suite` caches its compiled DAG. We decided that cost is tiny compared to the certainty we buy.

---

## Q5: Three error categories. Why not two, or five?

The categories are `TransientError`, `PermanentError`, `FatalError`. The question is: what makes this the right partition?

The partition is driven by **engine behavior**, not by origin. That's the key insight. We don't classify errors by *what kind of thing went wrong* — we classify them by *what the engine should do when it sees one*.

- `TransientError` → retry with backoff. This is the only category the retry loop ever catches.
- `PermanentError` → mark this task failed, let the run continue. A 4xx from the API, a schema violation, a missing template variable — all the same to the engine: give up on this task, keep going.
- `FatalError` → abort the whole run. Storage is unavailable, an invariant is violated, the process cannot make forward progress safely.

**Why not combine `Permanent` and `Fatal`?** Because the blast radius matters. A single task hitting a `PermanentError` should not end the run — you still want scores for the other 999 tasks. A corrupted storage file should end the run, because continuing would produce data you can't trust.

**Why not split `Transient` into rate-limit vs. server-error vs. network?** We considered this. The argument for splitting is that you might want different backoff strategies per cause. The argument against is that the retry loop doesn't actually need to know the cause — it needs to know "is it worth trying again?". The specific cause lives in `error.context` for debugging; the category drives behavior. Keeping categories minimal means fewer places in the codebase have to know about them.

**Provider errors get a subtype each** (`ProviderTransientError`, `ProviderPermanentError`). This is where the taxonomy meets the provider boundary. The rule: a provider translates its native exception (httpx status code, connection error) into one of these two before the error leaves `complete()`. User code never sees `httpx.HTTPError`. This is a hard invariant — it's what makes the retry loop sound.

---

## Q6: Why this pipeline DSL?

The user writes:

```python
pipeline=[
    llm_agent("solver", ...),                               # stage 0
    [llm_judge("correctness", ...), rule_judge("format", ...)],  # stage 1, parallel
]
```

We considered three alternatives and picked this one.

**Alternative A: Explicit DAG builder.**

```python
solver = Node(...)
judge_a = Node(...).after(solver)
judge_b = Node(...).after(solver)
pipeline = DAG([solver, judge_a, judge_b])
```

Pros: totally explicit, arbitrary topologies. Cons: four lines to express what the nested list does in two, and the `.after(...)` plumbing leaks into user code. Rejected.

**Alternative B: Decorator graph.**

```python
@stage(0)
def solve(task): ...

@stage(1, parent="solve")
def judge(task, output): ...
```

Pros: feels Pythonic. Cons: "Pythonic" here is code smell — we'd be inventing a decorator DSL that Python's import machinery then has to make sense of. Rejected.

**Alternative C: Nested list (chosen).**

Pros: the structure of the declaration mirrors the structure of the execution. You read it top-to-bottom and that's the order it runs. A nested list is "run in parallel here." No imports needed, no special terms, no `.after()` calls.

The cost is that fan-in is awkward — if two agents feed into one judge, the judge has to disambiguate with `parent="solver_a"`. We decided that cost is acceptable because fan-in is rare (the common case is one solver, many judges) and because the error at compile time is crisp when you get it wrong.

**Why we validated aggressively at compile time.** The `compile_pipeline` function rejects empty pipelines, duplicate node IDs, judge-only first stages, and fan-in-without-disambiguation. All of these could be caught at runtime with a panic, but the user experience is dramatically better when `Suite(...)` fails at import time with a pointer to the exact problem. A failed run at task 500 because of a pipeline-wiring bug is the worst possible outcome — you've already paid for 500 LLM calls and the error could have been caught before any code ran.

---

## Q7: Why compile the DAG eagerly at `Suite` construction?

The pattern is:

```python
suite = Suite(name="...", tasks=[...], pipeline=[agent, [judge_a, judge_b]])
# If this line returns, the pipeline is valid.
```

The alternative is lazy compilation: build the `ResolvedDAG` on the first call to `engine.run(suite)`.

**Why eager wins.** The point of a `Suite` is to be a declaration. A declaration that is syntactically valid but semantically broken is worse than a declaration that throws immediately. With eager compilation, the user's suite file either imports cleanly or it doesn't; there's no "this looks fine but will blow up in production" state.

There's also a secondary benefit: the compiled DAG is available for inspection (`suite.dag.topo_order()`, `suite.dag.judges()`) *before* a run starts. This makes it easy to write pre-flight tooling that checks a suite's shape without executing it.

**The cost is a slightly unusual `__init__`.** `Suite` extends Pydantic's `BaseModel` but does compilation in its own `__init__` override, then caches the `ResolvedDAG` via `object.__setattr__` because frozen models don't let you assign to properties normally. We disliked this enough to make it one of three places in the codebase that uses the escape hatch — but the user experience is worth it.

---

## Q8: How did we decide the event bus semantics?

This was the most architecturally load-bearing decision in the whole library. Get it wrong and either the engine is slow, or events drop silently, or subscribers race each other. We weighed three options:

| Option | Description | Back-pressure | Multi-subscriber | Verdict |
|--------|-------------|---------------|------------------|---------|
| A | Single bounded queue + background distributor task | Yes (one lock point) | Yes (distributor fans out) | More moving parts, same outcome |
| B | Per-subscriber bounded `anyio` memory object streams, publisher fans out | Yes (publisher blocks per sub) | Yes | **Chosen** |
| C | Callback list (no streams) | **No — callbacks block publisher, or drop if async-scheduled** | Yes | Silent event loss possible |

The decision comes down to what we believe about the relationship between the publisher and the slowest subscriber.

We believe: **a subscriber that can't keep up should throttle the publisher**, not drop events. The classic example: storage is SQLite, SQLite is synchronous, SQLite is sometimes slower than the engine can publish. If we drop events under pressure, storage's view of the run diverges from the engine's view of the run. Resume becomes unsound. That's a correctness bug disguised as a performance optimization.

So: back-pressure, always. The publisher blocks if any subscriber's buffer is full. The engine runs no faster than its slowest observer. This is exactly the property we want — it means the subsystem that's trying to keep up with the truth always can.

**Why per-subscriber streams specifically?** Because fan-out under a single queue requires a distributor task, which means one more concurrent actor in the system, which means one more potential deadlock site. With per-subscriber streams, the publisher iterates the subscriber list once under a lock (for `seq` assignment) then fans out. It's about 40 lines of code total. The simpler thing that works.

**Monotonic `seq` is authoritative at the bus.** We stamp `seq` on publish, overwriting whatever the caller put there. Having two sources of truth for event ordering is how you end up debugging why your audit log is non-sequential at 2am.

---

## Q9: Why is storage a subscriber and not a direct dependency?

The spec says "Storage is a subscriber, not a dependency of the engine." We enforced this literally: `engine.py` has zero imports from `storage/`. The import-linter contract makes it impossible to add one.

**Why the hard line?**

If the engine calls `storage.save_task_result(...)` directly, then:

- Every test of the engine has to stub or provide storage.
- Storage becomes part of the engine's failure surface. A storage hiccup becomes a task failure.
- A future "in-memory" storage, or "ship results to S3" storage, or "skip storage entirely" mode requires changes to the engine.
- The event stream and the persisted state can drift. What if the engine saves to storage but fails to publish the event? Or vice versa? Every consistency bug between observable state and persisted state has to be audited.

When storage is a subscriber:

- The engine has one output: events. Tests of the engine just collect events.
- Storage is optional. Run without it for experiments; add it for production.
- Any other subscriber (metrics aggregator, live dashboard, SSE relay) gets equal standing with storage.
- There is exactly one source of truth: the event stream. Persisted state is derived from it.

**The cost of this decision.** Events have to carry enough information to reconstruct state. `task_finished` events include the full serialized `TaskResult` in their payload. That's a few hundred bytes per task. For a 10,000-task run, that's 2 MB of event payload — negligible on any modern storage, but worth noting.

**The payoff.** `storage.attach(bus)` is 30 lines. It subscribes, reads events, applies them. That's the entire coupling. A second storage backend would be another 30 lines, zero changes to the engine.

---

## Q10: What made us confident resume is correct?

Resume is the feature that either makes the library trustworthy or makes it a liability. A resumed run that produces subtly different results than a clean run is the worst kind of bug — it'll be found by a user six months later, on a run they can't reproduce.

We reasoned about correctness in two steps.

**Step one: the invariants.** We wrote them down before writing the test:

1. No task is scored twice across (original + resumed) runs.
2. No task is silently dropped.
3. A resumed run's results equal a clean run's results on the same seed.

**Step two: the mechanism that makes each invariant true.**

*Invariant 1 (no double-scoring).* Storage writes each `TaskResult` transactionally on `task_finished`. The engine, on resume, calls `run.completed_task_ids` which pulls from storage, then filters `pending = [t for t in suite.tasks if t.id not in completed]`. A task can only appear in `pending` if its `ok` row is absent from storage. Mechanism: atomic commit + set difference.

*Invariant 2 (nothing dropped).* Every task in the suite is either in `completed_ids` (already done) or in `pending` (about to run). The union is exhaustive by construction — both sets come from `suite.tasks` via a membership test. Mechanism: partition of a set is a partition.

*Invariant 3 (seed-equality).* The engine's only sources of randomness are (a) LLM calls (which the mock provider makes deterministic in test) and (b) retry jitter (which is seeded on `hash((run.seed, task.id, node.id, attempt))`). A resumed run uses the same seed as the original. Therefore any task re-run on resume sees the same deterministic jitter sequence as it would in a clean run. Mechanism: pure function of seed.

**Then we wrote the test that tries to break it.** The fault-injection test kicks off eight tasks with concurrency=2 and 50ms latency per task, triggers `stop_event` at 70ms so some tasks complete and some don't, persists the partial run, resumes it, and verifies all three invariants hold. It also runs the suite cleanly and asserts per-task score equality with the resumed run.

This is the test we'd want to see as a skeptical reader. It's the test that turned resume from "I think it works" into "I know it works."

---

## Q11: Why task-level concurrency rather than node-level?

The engine has a semaphore that bounds how many *tasks* are in flight concurrently. Within a task, fan-out at a DAG layer is unbounded (naturally capped by stage width).

**We considered node-level scheduling.** Treat every DAG node for every task as a schedulable unit, throw them all at a worker pool.

Two reasons we didn't:

1. **It doesn't match how users think.** A user who says `concurrency=8` is thinking "I want eight problems being worked on at once." They're not thinking about the distinction between solver invocations and judge invocations. Task-level concurrency gives them exactly that mental model.

2. **Node-level scheduling adds bookkeeping.** You have to track which nodes are ready (parents done), which are running (for back-pressure), which are blocked (parents pending). You have to handle the case where two tasks need the same judge, and one is pre-empted by the other. It's not hard, but it's extra code with extra edge cases, and the only payoff is better utilization in a pathological case (one very expensive judge feeding many cheap tasks) that we don't have evidence of.

The task-level approach is: each task walks its own DAG, waits for its own judges, commits its own result. If you want more throughput, raise the semaphore. If you want less, lower it. One dial.

**What we'd do later.** If a user hits a real bottleneck — a judge that's 100× slower than its siblings — the right fix is a per-agent semaphore, not a restructured scheduler. We sketched the shape of it mentally but didn't build it; no test exercises it and YAGNI.

---

## Q12: Why retry at the node, not the task?

Consider: a task has a solver, then a correctness judge, then a style judge. The style judge gets rate-limited. What happens?

With **task-level retry**: the whole task re-runs. The solver fires again (another LLM call you paid for). The correctness judge fires again (another LLM call). The style judge fires again (and maybe still rate-limited).

With **node-level retry** (chosen): the style judge re-runs. The solver and correctness judge's results are reused from this task's run. One extra LLM call, not three.

The payoff scales with pipeline depth. For a two-stage pipeline the waste is tolerable; for a five-stage pipeline it's catastrophic. Task-level retry would burn LLM budget every time a sixth-stage judge flakes.

**The subtle requirement this creates.** Each node's retry loop has to know it's idempotent with respect to the upstream state. In practice this is fine — agent/judge calls are stateless to their inputs — but if you had side-effecting nodes (writing to a DB, sending a webhook) you'd need to think harder. We don't, today, have such nodes; the design leaves room to add an `idempotent: bool` flag on the protocol if we ever do.

**Jitter is seeded.** The RNG is `random.Random(hash((run.seed, task_id, node_id, attempt)))`. Four facts matter here:
- `run.seed` is constant across a run → same run, same jitter.
- `task_id` + `node_id` → different tasks / different nodes get uncorrelated jitter.
- `attempt` → each retry gets fresh jitter (so two retries aren't in lockstep).
- `hash(...) & 0xFFFFFFFF` → fits in the 32-bit seed Python's `Random` accepts.

Deterministic on purpose; uncorrelated on purpose. That's the sweet spot.

---

## Q13: Why no vendor SDKs for Anthropic / OpenAI?

We use `httpx` directly. The spec's required-deps list did not include `anthropic` or `openai` SDKs, which was a signal, but the real reasoning is architectural.

**SDK dependencies have their own universes.**

The `anthropic` SDK pins its own `httpx` version. So does `openai`. So does `fastapi` (transitively). If you have all three, the resolver has to find a single `httpx` version all three accept. That's fine until one of them releases a breaking change and the resolver can't find a solution without downgrading another.

Our stance: we speak the HTTP protocol. The HTTP protocol is stable. Anthropic's Messages API and OpenAI's Chat Completions API are stable at the wire level — they've been stable for years. We write maybe 80 lines per vendor to translate requests and responses. In exchange we have zero transitive dependency churn.

**Testing gets easier.** `httpx.MockTransport` lets us intercept every request with a handler function. The test file for the providers exercises:
- request shape (payload, headers, URL),
- response parsing (text, token counts, finish reason),
- error translation for 429, 4xx, 5xx, network errors,
- construction-time failure when env vars are missing.

None of this uses `unittest.mock` or SDK internals. It's all behavior at the HTTP boundary. If Anthropic ships a new field tomorrow, we won't get surprise behavior changes from an SDK update we didn't audit.

**What we give up.** Retry middleware, streaming helpers, tool-use helpers, and whatever clever thing the SDK teams ship next month. For this library's purpose — single-shot completions with token accounting — we don't need any of it. If a user does, they can wrap our `Provider` protocol around a SDK client; the protocol is eight lines.

---

## Q14: Why is secret redaction a correctness test, not a code review concern?

The test proves: set `ANTHROPIC_API_KEY=sk-ant-xxx`, emit a log line that contains the key, assert the rendered output doesn't contain `sk-ant-xxx`.

We could have just said "don't log secrets." Instead we built a structlog processor that walks every record (including nested dicts, lists, tuples) and replaces any known-secret substring.

**Why the belt-and-suspenders?**

Provider adapters don't log keys. That's rule one, enforced by review. But:

- Users write their own code on top of the library. They might log a request dict that happens to contain a header.
- Exception traces can include argument values from frames. A `repr(request)` that happens to include an auth header ends up in a log.
- `structlog` renders the whole event dict. Everything bound via `log.bind(...)` is a candidate for rendering.

The processor means: however a secret ends up in the event dict, it can't make it to the rendered output. The test proves this for direct strings, nested dict values, and list items — the three ways a secret could travel.

**This is the kind of invariant that belongs in code, not in checklists.** A code-review rule depends on the reviewer catching every case forever. A processor + a test catches every case now and forever. The ROI on twenty lines of code is enormous.

---

## Q15: How did we keep the layered architecture honest?

The module order in the spec is prescribed:

```
types / errors  →  events  →  providers  →  agents  →  pipeline  →  storage  →  engine  →  cli / server
```

Modules may only import from modules below them. Violations fail CI.

**We enforced this with `import-linter` contracts in `pyproject.toml`.** Two contracts:

1. A `layers` contract listing the exact module order. Any cross-layer import fails.
2. A `forbidden` contract explicitly naming `types` and `errors` as true leaves: they may not import anything else in the project, and may not import `httpx`, `sqlalchemy`, `fastapi`, or `typer`.

**Why the second contract is necessary when the first exists.** The layers contract prevents `types.py` from importing `pipeline.py`. It doesn't prevent `types.py` from importing `httpx`. Separately forbidding external libraries at the leaf layer means `types.py` *stays* a leaf at the library-dependency level, not just the intra-package level. The day someone writes `from httpx import URL` into `types.py` because it's convenient, CI fails and the PR can't merge.

**The payoff.** We never had to think about layer violations after M0. We wrote the code, and if we accidentally reached upward, CI told us at the next push. The architecture is not a convention maintained by good intentions; it's a property the tooling will not let us break.

**Consequence.** When writing `engine.py`, we had to serialize `TaskResult` into event payloads because the engine couldn't call storage directly. This felt awkward for a minute — it's strictly more code than just calling `storage.save(result)`. Then we realized: this is the force function that keeps the event stream complete. The constraint isn't an annoyance; it's the thing driving the design to be sound.

---

## Q16: Where did we hit real friction, and how did we climb out?

Five moments in the session where the design or the implementation pushed back. The log in git will show these as small back-and-forth commits; the lesson in each is worth preserving.

### 16a. The event bus teardown dance

**Problem:** First fault-injection test failed because `run_finished` events never reached storage. The `attach` context was exiting before the drain task had processed the buffer.

**The wrong instinct:** close the subscriber stream in `__aexit__` so the drain task exits. This is what I wrote first. It's wrong because `stream.aclose()` on the receive side discards pending events.

**The right model:** the drain task only exits cleanly on `EndOfStream`, which only fires when the *send* side closes — i.e., when the *bus* is closed. So the contract became: close the bus before exiting the storage context. The context's `__aexit__` waits for the drain task's task group to complete, which happens naturally once the bus is closed.

**The lesson:** when async teardown feels off, ask "who is signaling 'done' to whom?" — and make sure the close direction matches the data-flow direction.

### 16b. B023: loop-captured closures

**Problem:** ruff flagged `_run_node_in_layer` defined inside `for layer in dag.layers:` as capturing loop-varying locals (`layer_lock`, `layer_outputs`).

**Why it was actually fine today but wrong tomorrow:** the closure only ran inside the same iteration's `async with anyio.create_task_group()` block, so it captured the current iteration's locals. But that invariant wasn't visible in the code — any future refactor that deferred the tasks across iterations would silently break it.

**The fix:** extract `_run_layer` as a method taking `layer`, `outputs`, etc., as explicit parameters. Cleaner, passes the linter, survives any future edit.

**The lesson:** ruff's style checks aren't always style. Some of them encode invariants that your code depends on without saying so.

### 16c. mypy and variance in union types

**Problem:** `tuple(stage)` where `stage: Agent | Sequence[Agent]` returned `tuple[Agent | Sequence[Agent]]` — not the `tuple[Agent, ...]` we wanted.

**Why:** mypy doesn't narrow element types through `isinstance(stage, (list, tuple))`. The narrowed type is `list | tuple`, not `list[Agent] | tuple[Agent, ...]`. So the element type stays the union.

**The fix:** annotate `items: list[Agent] = list(stage)` in the list/tuple branch, and `single = cast(Agent, stage)` in the single-node branch. The cast is honest — we've already verified `hasattr(stage, "id") and hasattr(stage, "run")`.

**The lesson:** when mypy surprises you on a generic type, the fix is usually an explicit annotation at the point where the type is "obvious" to the reader. The cast isn't a workaround; it's the proof that the runtime check we already wrote is complete.

### 16d. FastAPI's `on_event` deprecation

**Problem:** We used `@app.on_event("startup")`. FastAPI 0.111+ issues a `DeprecationWarning`. Our pytest config has `filterwarnings = ["error"]`. First server test failed.

**Why we had `filterwarnings = ["error"]` in the first place:** because deprecation warnings in tests rot quietly. They stop being warnings and start being errors when the library ships a breaking change, and by then you're on the critical path. Treating them as errors now means you pay the migration cost in small increments.

**The fix:** migrate to the `lifespan` context-manager pattern. Five lines.

**The lesson:** `filterwarnings = ["error"]` is one of the highest-ROI two lines of config you'll ever write.

### 16e. Coverage script filename mismatch

**Problem:** First run of `scripts/check_core_coverage.py` against `coverage.xml` reported "no core lines counted" — the script was matching `evalforge/types.py` but the XML reported `types.py` (stripped prefix).

**The subtle issue:** `coverage.py` uses package-relative paths, not workspace-relative paths. The `<package>` elements in the XML have `name="."`, `name="providers"`, `name="storage"` — and the classes inside use filenames relative to the package.

**The fix:** match on the `filename` attribute, which is `types.py` (or `storage/sqlite.py`), not `evalforge/types.py`.

**The lesson:** every tool has its own idea of what "path" means in its output format. Assume the format; verify with a 30-second script that prints what it actually sees.

---

## Q17: What did we deliberately not build?

A partial list, each with reasoning:

- **Per-agent concurrency override.** Suite-level `concurrency` is the one dial today. An agent with a tight rate limit could benefit from its own semaphore, but no test exercises it and no user has asked. Adding it later is additive — no existing test breaks.
- **Live SSE streaming from an in-flight run.** `GET /runs/{id}/events` replays from storage. Fan-out from the live bus to HTTP clients requires per-connection subscribers and their own buffer management; it's more complex than the core and can land when real users need it.
- **Output caching by `(agent_id, task_id, input_hash)`.** Flagged as a stretch goal in the spec. The caching key needs to include the agent's version / prompt hash, or stale cache hits become a correctness bug. We wanted the first version to be simple and trustworthy; caching goes in version two.
- **Result diffing UI.** `evalforge diff` prints per-task score deltas to the terminal. A side-by-side HTML view would be nicer. It's a stretch goal; the CLI text form is sufficient for the spec's acceptance criteria.
- **Signal handlers inside the engine.** `Engine.run` takes an `anyio.Event` stop signal. Production code that wants real SIGINT handling wires `anyio.open_signal_receiver` to flip the event. We kept the signal mechanism out of the engine itself so tests don't have to send real signals to verify shutdown behavior — and so the library composes into processes that already have their own signal policy.
- **Retry policies expressed as predicates.** Each retry policy is a simple `(max_retries, base_backoff, max_backoff, jitter)` tuple. A richer form (retry on specific error contexts, custom backoff curves) would be straightforward to add; none of our scenarios demanded it.

Each of these is a door we left unlocked but didn't walk through. That was a conscious choice — every additional abstraction is a cost paid by every future reader.

---

## Q18: If someone forked this tomorrow, where would they struggle?

Honest answer, three places.

**1. The `Suite.__init__` escape hatch.** Frozen Pydantic models are usually immutable, but `Suite` caches its compiled `ResolvedDAG` via `object.__setattr__` because the compilation depends on runtime agent instances, not on the Pydantic validator's input. A reader coming to `suite.py` cold will find that surprising. We commented it and kept it to one place. If pydantic ships proper `cached_property` support for frozen models, we'd migrate immediately.

**2. The event-payload-as-carrier pattern for storage.** The engine publishes `run_started` with the full `Run` serialized inside the payload, and `task_finished` with the full `TaskResult`. This is how storage reconstructs state from events alone. A reader might initially think "why not just have the engine call storage?" — the answer is in Q9 above, but it's not visible in the code. We considered adding a dedicated `Event.result: TaskResult | None` field instead of stuffing it in the payload dict; we preferred the payload dict because it keeps `Event` narrow and typed.

**3. `storage.attach(bus)` bus-close semantics.** The caller has to close the bus inside the `attach` context, or the drain task won't exit cleanly. We documented this, and `attach` closes the bus itself as a safety net if the caller forgot. A reader who assumes "context-managed resources close themselves" will be mildly surprised. It's the correct behavior (the alternative discards buffered events), but the mental model takes a minute to load.

---

## Milestone recap

| M | What was built | Gates | Tests (cumulative) |
|---|----------------|-------|---------------------|
| M0 | Tooling: `pyproject.toml`, ruff, `mypy --strict`, `import-linter`, pre-commit, GitHub Actions CI. Empty package with smoke test. | ✅ | 1 |
| M1 | Frozen Pydantic domain types; `EvalforgeError` hierarchy; `Agent`/`Judge` protocols and factories; `compile_pipeline` → `ResolvedDAG`; `Suite` with eager compile. | ✅ | 28 |
| M2 | `EventBus` with per-subscriber bounded streams; `MockProvider`; the execution engine with bounded concurrency, per-node retry, seeded jitter, graceful shutdown via stop-event. | ✅ | 38 |
| M3 | `Storage` Protocol; `SQLiteStorage` with WAL, transactional task writes, migration runner; event-driven persistence via `storage.attach(bus)`; fault-injection resume test proving the three resume invariants. | ✅ | 41 |
| M4 | `AnthropicProvider` and `OpenAIProvider` (built directly on httpx, no vendor SDK); HTTP error translation at the boundary; pluggable pricing table; `structlog` pipeline with recursive secret redaction. | ✅ | 52 |
| M5 + M6 | `_loader.load_suite`; Typer CLI (`run`, `show`, `list`, `diff`); FastAPI server with lifespan, `POST /runs`, `GET /runs`, `GET /runs/{id}`, `GET /runs/{id}/events`; `examples/math_suite.py` runnable against the mock. | ✅ | 55 |
| M7 | README, CHANGELOG (v0.1.0 tagged), this session log. | ✅ | 55 |

---

## Final state

- **55 tests**, ~2s total under `pytest-randomly` ordering.
- **mypy --strict**: 19 source files, 0 errors, 0 unjustified `# type: ignore`.
- **ruff check + format**: clean.
- **import-linter**: 45 files, 139 dependencies analyzed, both contracts (layered architecture + types/errors as leaves) KEPT.
- **Core coverage**: 92.33%, floor is 90%.
- **CI matrix**: Python 3.11 and 3.12, same gate sequence.
- **7 commits on `main`**, one per milestone boundary, each commit green.

The library is what the spec asked for. More importantly, the reasoning behind what the spec asked for is preserved here — in a form that a future maintainer can challenge, revise, or build on.

If the question a reader brings to this repo is "why is it shaped this way?", this document is the answer.
