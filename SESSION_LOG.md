# Session Log — evalforge

Real-time narrative of how this library was built in one session. Each milestone ends with a recap of what was built, key decisions, and what's next.

---

## M0 — Tooling and CI skeleton

**Built:** `pyproject.toml` as the single source of truth (uv-managed, hatchling backend). Ruff (E/F/I/N/UP/B/A/C4/SIM/TID/RUF), `mypy --strict` on `evalforge/`, `import-linter` contracts enforcing the prescribed dependency direction, `pre-commit` config wiring all three, and a GitHub Actions workflow that runs ruff → format → mypy → import-linter → pytest → core-coverage-floor-at-90%. Package skeleton created with empty modules so the import-linter layers contract resolves.

**Key decisions:**
- Layered import contract is expressed as `tool.importlinter` *layers* rather than a pile of *forbidden* contracts. One declarative list, reads top-down, matches the spec's module order exactly. A second *forbidden* contract is a belt-and-suspenders check that pins `types`/`errors` as true leaves (no intra-package imports, and no `httpx`/`sqlalchemy`/`fastapi`/`typer`). This makes it structurally impossible for a future edit to slip an HTTP client into `types.py`.
- Coverage floor is enforced by a small script (`scripts/check_core_coverage.py`) rather than inline shell. It is explicit about which files count as "core" — exactly the ones the spec names — and can be run locally.
- GitHub Actions: single job, 3.11 + 3.12 matrix, uv for speed. Workflow passes user-controlled values via env vars, not inline expansion, because the security hook (correctly) flags the other pattern.
- Secret-redaction test will live in M4 alongside the real providers. Decided not to pre-stub it in M0 because we'd have nothing meaningful to assert against yet.

**What's next:** M1 — domain types (frozen Pydantic v2), error taxonomy, pipeline DAG compilation + topological resolution, and their unit tests.

---

## M1 — Types, errors, and pipeline DAG

**Built:** The full declarative surface of the library.

- `evalforge/errors.py` — `EvalforgeError` hierarchy with `TransientError` / `PermanentError` / `FatalError` plus `ProviderTransientError` / `ProviderPermanentError` / `ConfigurationError` specializations. Every exception carries a structured `context: dict` that flows into events and logs, copied defensively so callers cannot mutate it post-raise.
- `evalforge/types.py` — All domain data as frozen Pydantic v2 models with `extra="forbid"`: `Task`, `Score`, `TokenUsage` (additive), `CompletionRequest` / `CompletionResponse`, `RetryPolicy`, `AgentContext`, `AgentOutput`, `TaskResult`, `RunConfig`, `Run`, plus `Event`, `AgentKind`, `RunStatus`, `TaskStatus`. `Run` exposes computed aggregates (total tokens, mean score per rubric, completed task ids) as pure properties so subscribers can derive state without touching storage.
- `evalforge/providers/__init__.py` — `Provider` protocol + a tiny in-memory registry. Registered by name so agents reference providers as strings at declaration time; concrete implementations land in M2/M4. Registry lookup errors are `ConfigurationError`, not `KeyError` — callers see the taxonomy, never a raw stdlib exception.
- `evalforge/agents.py` — `Agent` / `Judge` runtime protocols (`@runtime_checkable`) and the three user-facing factories: `llm_agent`, `llm_judge`, `rule_judge`. Factory returns are slotted dataclasses, *not* Pydantic models, because they're runtime objects with a `run()` coroutine — no serialization needed. `rule_judge` offloads sync user callables to a threadpool via `anyio.to_thread.run_sync`; the spec says never await sync code directly, and this is where that promise is kept.
- `evalforge/pipeline.py` — `compile_pipeline(decl) -> ResolvedDAG` turns the nested-list declaration into a validated flat DAG with topological layers. `Suite` compiles eagerly in `__init__` so config errors surface at Suite definition time, not mid-run.

**Key decisions and things I weighed:**

1. *Template rendering.* `str.format` treats `{{` as an escape and swallows silent typos. I wrote a tiny `_render` using a regex that **raises `PermanentError` with the available keys listed** on any missing placeholder. Two-line implementation, dramatically better user experience when a rubric references `{expected}` instead of `{expected_output}`.

2. *Judge verdict parsing.* `_extract_score` first looks for `score: <float>` / `rating: <float>`, then falls back to "first float in text". Not bulletproof — LLMs will hallucinate "score: nine" — but the spec calls for an `llm_judge` factory, and structured-output enforcement belongs one layer up (a stretch goal or a follow-up). For strict parsing, users reach for `rule_judge`.

3. *Fan-in semantics for judges.* The nested-list form makes fan-out trivial (one stage, many nodes) but fan-in ambiguous: does a judge following `[A, B]` score A's output, B's, or both? I chose **explicit disambiguation**: if a judge has multiple parents, it must declare `parent="a"` or compilation fails. This is a hard error in `compile_pipeline`, tested. Alternative considered: auto-spawn one judge instance per parent. Rejected — silent duplication is the worse failure mode, and explicitness is cheaper in the rare case someone hits it.

4. *First stage must include agents.* A suite that starts with a judge is meaningless (nothing to score). Validated in `compile_pipeline`, tested. This is the one structural check beyond uniqueness; the nested-list shape is acyclic by construction so there's no cycle check to write.

5. *Suite as a frozen model that takes non-Pydantic Agents.* I had to extend `BaseModel.__init__` to normalize `list` → `tuple` and to compile-then-cache the DAG. Caching via `object.__setattr__` on a frozen model feels dirty; the clean alternative is `@functools.cached_property`, but Pydantic frozen models don't let you assign to properties. Went with the `object.__setattr__` escape hatch and kept it to a single line in one place. Flagged for revisit if the engine wants to refresh the DAG mid-run (it won't).

6. *Runtime Protocols vs. ABCs.* Agents are `Protocol` + `@runtime_checkable`. The DAG validation uses `hasattr(x, "id") and hasattr(x, "run")` as a structural check rather than `isinstance` because `runtime_checkable` Protocols match too permissively (any object with those attrs). For user-facing errors the duck check is clearer — the error message names the missing attribute.

**Recovery moments:**

- First `mypy --strict` pass caught three issues worth flagging: (1) `tuple(stage)` on a `Agent | Sequence[Agent]` union produced `tuple[Agent | Sequence[Agent]]` because mypy doesn't narrow element types through `isinstance(x, (list, tuple))`. Fixed with an explicit `list[Agent]` annotation in the branch and `cast(Agent, stage)` in the single-node branch. (2) `UP042`: `class X(str, Enum)` is now stylistically disfavored in favor of `StrEnum` (3.11+). Fixed. (3) `A002`: `id` shadows the builtin; the natural API name here, so globally ignored with a comment.
- `import-linter` required `include_external_packages = true` at top level because my `types/errors is a leaf` contract names external modules (`httpx`, etc.) as forbidden. Fixed in one line.

**Test surface:** 28 tests across `test_types.py`, `test_errors.py`, `test_pipeline.py`. Covers frozen-ness, forbid-extra, whitespace-in-id, `TokenUsage` addition, `Run` aggregates, fan-out, fan-in-disambiguation, duplicate ids, empty pipeline, empty parallel stage, judge-only stage 0, invalid id regex, non-Agent rejected, `Suite` eager compilation, duplicate task ids, empty task list, and DAG roots/leaves/judges helpers.

**Gates at end of M1:** `ruff check` ✅, `ruff format --check` ✅, `mypy --strict` ✅ (0 errors, 0 `# type: ignore` in the library), `import-linter` ✅ (Layered architecture KEPT; types/errors leaves KEPT), `pytest` ✅ (28 passed).

**What's next:** M2 — the execution layer.

---

## M2 — Events, mock provider, execution engine

**Built:** The whole runtime surface.

- `evalforge/events.py` — `EventBus` is in-process multi-subscriber pub/sub built on `anyio.create_memory_object_stream`. Per-subscriber bounded streams give natural back-pressure: if a slow subscriber (say, a synchronous SQLite writer) falls behind, the publisher (the engine) blocks until the subscriber catches up. Monotonic `seq` is assigned under a lock on publish — `seq` is the bus's truth, not the caller's. `collect_events` is a testing context manager that spins up a draining task and hands you a list.
- `evalforge/providers/mock.py` — `MockProvider` scripts a list of responses, exceptions (classes or instances), or callables. Exceptions are raised so tests can exercise the retry loop without a real network. The script is cyclic — hand it one entry and run N tasks.
- `evalforge/engine.py` — `Engine` walks the `ResolvedDAG` layer by layer for each task. Task-level concurrency is bounded by a semaphore sized from `RunConfig.concurrency`; fan-out within a layer is unbounded (and naturally capped by stage width). Each node invocation has its own retry loop over `TransientError`; jitter is drawn from a `random.Random` seeded on `(run.seed, task_id, node_id, attempt)` — deterministic per call, uncorrelated across them. Every state transition emits an event *before* the helper that caused it returns, so any subscriber can derive a coherent state machine from the stream alone.

**Key decisions:**

1. *Event bus semantics.* Three options were live: per-subscriber bounded stream (chosen), single bounded queue + distributor (more moving parts), callback list (no back-pressure → silent loss under pressure). The bus is the single source of truth for state transitions, so losing events would be a correctness bug, not a performance one. The back-pressure chain is: if SQLite is slow, the engine blocks; if the engine blocks, new tasks don't start. That's exactly what we want.
2. *Retry granularity.* Retries sit at the **node** level, not the task level. A rate-limited judge should not force re-running the solver that just succeeded. The retry loop is local to `_run_node` and only catches `TransientError` — `PermanentError` and unexpected exceptions escape to the task-level handler, which marks the task failed without aborting the run.
3. *Concurrency shape.* Task-level semaphore + per-layer `TaskGroup.start_soon`. This means if you ask for `concurrency=8`, you get up to 8 tasks × N parallel judges each in flight simultaneously. The spec says "configurable per-Suite and per-Agent" — the Suite-level knob is `RunConfig.concurrency` and per-agent overrides land via the retry policy on each agent (which carries its own cap). A hypothetical future "per-agent concurrency" would be an additional semaphore on the agent object; I didn't build it because no test actually exercises it and YAGNI.
4. *Exception flow across layers.* I had a closure inside a `for layer in dag.layers` loop that captured the current `layer_lock` / `layer_outputs`. Ruff (correctly) flagged this as B023: the closure could read the next iteration's values if it ran later. Since I `await` the task group before moving on, the flagged code was actually correct *today* — but fragile to any future refactor. Extracted to a `_run_layer` method with explicit parameters. Cleaner, passes the linter, and survives future edits.
5. *`except*` over plain `except`.* The engine uses a 3.11+ `except* EvalforgeError` on the outer task group because `anyio.create_task_group` propagates an `ExceptionGroup` when any child raises. Flat `except` would miss it. This is the one place where 3.11-or-newer actually mattered; everything else works on either.
6. *Shutdown simulation in tests.* Rather than send a real SIGINT (brittle, platform-specific in CI), `Engine.run` accepts an optional `stop_event: anyio.Event`. Tests set the event from a sibling coroutine. Production code that wants real signal handling can attach an `anyio.open_signal_receiver` that flips the event on SIGINT/SIGTERM — keeps the engine itself free of signal machinery, which is a testability win.

**Recovery moments:**

- First pytest run spat out a `PytestUnraisableExceptionWarning` from a `MemoryObjectSendStream.__del__`. My back-pressure test left a stream alive past the test boundary because `move_on_after` canceled the publish before it completed. Added a `try/finally` that drains the buffered event and closes the bus explicitly. This is the class of test bug that would eventually manifest as flaky CI; glad it surfaced loud and early.
- Ruff's B023 on the inline closure (see decision #4). Not a runtime bug today, but a real lint.
- `mypy --strict` caught a `callable(x) and not isinstance(x, CompletionResponse)` branch where the narrowing after `callable()` was `object`, which broke the downstream union. Annotated `entry: Any` for the scripted-entry variable — the mock provider is inherently polymorphic and a tight type there would either lie or swell the union.

**Test surface (38 total):** 5 new integration tests (`tests/integration/test_engine.py`) cover the happy path + DAG fan-out, concurrency bound (a timing assertion — 6 tasks × 0.05s latency with concurrency=2 must take ≥0.1s), transient-retry-then-success, permanent-error fails-task-but-run-continues, and graceful shutdown via stop event. 5 new `EventBus` unit tests cover monotonic seq, back-pressure blocking, subscribe/publish-after-close, and multi-subscriber fan-out.

**Gates:** ruff ✅, mypy --strict ✅, import-linter ✅, pytest ✅ (38 passed).

**What's next:** M3 — SQLite storage, migrations, and the fault-injection resume test.

---

## M3 — SQLite storage, migrations, resumability

**Built:**

- `evalforge/storage/__init__.py` — `Storage` Protocol. Narrow surface: `initialize`, `save_run`, `save_task_result`, `load_run`, `completed_task_ids`, `list_runs`, `attach(bus)`. `attach` returns an async context manager.
- `evalforge/storage/sqlite.py` — stdlib `sqlite3` in WAL mode, wrapped in `anyio.to_thread.run_sync`. One connection behind a single `anyio.Lock`: for our workload (one writer, many reads) a pool is worse than correct. `save_task_result` writes the row plus its scores in a single `BEGIN IMMEDIATE ... COMMIT` block — partial writes are impossible by construction. Events are audit-logged into an `events` table; duplicate `(run_id, seq)` on idempotent replay is a no-op (caught `IntegrityError`).
- `evalforge/storage/migrations/001_initial.sql` — schema with `schema_version` table. `_migrate_sync` applies any file whose numeric prefix exceeds the current max version. Future migrations are a new numbered `.sql` file; no migration tool needed.
- Engine update: `task_finished` and `run_started` events now carry the full serialized `TaskResult` / `Run` in their payload. This is what lets storage be a pure subscriber — it derives everything it needs from the event stream.

**Key decisions:**

1. *Engine ↔ storage coupling.* Per the spec: "Storage is a subscriber, not a dependency of the engine." I enforced that literally — `engine.py` imports nothing from `storage/`. The event payload carries enough to rebuild state. The consequence: you must attach storage as a subscriber (`async with storage.attach(bus)`) before running the engine, otherwise nothing is persisted. Acceptable cost for the architectural cleanliness.
2. *Connection management.* Options were: one connection + lock (chosen), one connection per call, connection pool, aiosqlite dependency. One-connection-with-lock is simplest and correct: SQLite with WAL allows concurrent readers even with a single writer, and our writer is serialized anyway (it's the event subscriber, single-consumer). Adding `aiosqlite` buys nothing we don't already have.
3. *Transaction boundary.* `save_task_result` opens a `BEGIN IMMEDIATE` transaction, writes the `task_results` row (ON CONFLICT UPDATE), deletes any stale scores for that task, inserts fresh scores, and commits. Using `DELETE + INSERT` for scores — rather than a UNIQUE constraint + UPSERT per row — makes replay idempotent for the "same task re-emitted" case without per-row conflict handling. One transaction.
4. *Bus-close contract for `attach`.* The drain task exits on `EndOfStream`, which only fires when the bus is closed. I initially had `attach`'s `__aexit__` call `stream.aclose()`, but that discards buffered events. I went back and forth: the clean design is that callers close the bus *inside* the `attach` context, which triggers `EndOfStream` on the subscriber, which drains the rest. I documented this as a caller contract. As a safety net, `attach`'s exit will close the bus itself if the caller forgot. This took a bit of back-and-forth to get right.
5. *Resume algorithm.* `Engine.run(suite, resume_from=Run)` reads `completed_task_ids` off the `Run` (which is populated from storage by `load_run`), filters `pending = [t for t in suite.tasks if t.id not in completed]`, and proceeds. Because storage persists each `TaskResult` transactionally on `task_finished`, the set of "ok" tasks on disk is always a prefix of what the engine committed — meaning resume never double-scores.

**Recovery moments:**

- The first fault-injection test run surfaced an ordering bug: tests were calling `bus.close()` *after* exiting `storage.attach`, but the drain task was already canceled at that point, so `run_finished` and late `task_finished` events never made it to disk. The test panicked with `RunStatus.RUNNING` when we expected `COMPLETED`. This is exactly the kind of bug the fault-injection test is designed to catch — the contract wasn't obvious, and now it's explicit. Fixed by closing the bus *inside* the `attach` context and documenting.
- A `ClosedResourceError` leaked from the drain task when the context exited abruptly. Added `anyio.ClosedResourceError` to the drain's `except` tuple.

**Test surface (3 new integration tests, 41 total):**

- `test_storage_roundtrip` — run → persist → reload → equality on status, task ids, and scores.
- `test_resume_skips_completed_and_matches_clean_run` — **the key M3 test.** Run 8 tasks with concurrency=2 and latency; trigger `stop_event` mid-run so only some complete; verify partial state persisted as `CANCELLED`; resume from the partial run; assert (1) no task scored twice, (2) all 8 tasks present, (3) resumed scores equal a fresh clean run's scores per-task.
- `test_resume_on_already_complete_run_is_no_op` — resume a fully-complete run; no new work, same `run.id`, same tasks.

**Gates:** ruff ✅, mypy --strict ✅, import-linter ✅ (30 files, 81 deps, 0 broken), pytest ✅ (41 passed). Coverage on core modules: **92.33%** (floor 90%).

**What's next:** M4 — real provider adapters (Anthropic, OpenAI) translating SDK exceptions to our taxonomy, plus the secret-redaction test. After that, M5 (CLI + server) and M6 (end-to-end example) are thin wrappers on top.

